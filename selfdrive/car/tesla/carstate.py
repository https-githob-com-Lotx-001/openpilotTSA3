import copy
from collections import deque
from cereal import car
from openpilot.common.conversions import Conversions as CV
from openpilot.selfdrive.car.tesla.values import CAR, DBC, CANBUS, GEAR_MAP, DOORS, BUTTONS, MODEL3_Y_BUTTONS
from openpilot.selfdrive.car.interfaces import CarStateBase
from opendbc.can.parser import CANParser
from opendbc.can.can_define import CANDefine

class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    self.model3_y = self.CP.carFingerprint in [CAR.TESLA_AP3_MODEL3, CAR.TESLA_AP3_MODELY]
    self.models_raven = self.CP.carFingerprint == CAR.TESLA_MODELS_RAVEN

    self.button_states = {button.event_type: False for button in (MODEL3_Y_BUTTONS if self.model3_y else BUTTONS)}
    self.can_define = CANDefine(DBC[CP.carFingerprint]['chassis'])

    # Needed by carcontroller
    self.msg_stw_actn_req = None
    self.hands_on_level = 0
    self.steer_warning = None
    self.das_control_counters = deque(maxlen=32)
    self.acc_enabled = None
    self.sccm_right_stalk = None
    self.das_control = None

  def update(self, cp, cp_cam, cp_adas):
    ret = car.CarState.new_message()

    # Vehicle speed
    ret.vEgoRaw = cp.vl["ESP_B"]["ESP_vehicleSpeed"] * CV.KPH_TO_MS
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    ret.standstill = (ret.vEgo < 0.1)

    # Gas pedal
    pedal_status = cp.vl["DI_systemStatus"]["DI_accelPedalPos"] if self.model3_y else cp.vl["DI_torque1"]["DI_pedalPos"]
    ret.gas = pedal_status / 100.0
    ret.gasPressed = (ret.gas > 0)

    # Brake pedal
    ret.brake = 0
    if self.model3_y:
      ret.brakePressed = cp.vl["IBST_status"]["IBST_driverBrakeApply"] == 2
    else:
      ret.brakePressed = bool(cp.vl["BrakeMessage"]["driverBrakeStatus"] != 1)

    # Steering wheel
    if self.model3_y:
      epas_status = cp.vl["EPAS3S_sysStatus"]
      self.hands_on_level = epas_status["EPAS3S_handsOnLevel"]
      self.steer_warning = self.can_define.dv["EPAS3S_sysStatus"]["EPAS3S_eacErrorCode"].get(int(epas_status["EPAS3S_eacErrorCode"]), None)
      steer_status = self.can_define.dv["EPAS3S_sysStatus"]["EPAS3S_eacStatus"].get(int(epas_status["EPAS3S_eacStatus"]), None)
      ret.steeringAngleDeg = -epas_status["EPAS3S_internalSAS"]
      ret.steeringRateDeg = -cp_adas.vl["SCCM_steeringAngleSensor"]["SCCM_steeringAngleSpeed"]
      ret.steeringTorque = -epas_status["EPAS3S_torsionBarTorque"]
    else:
      epas_status = cp_cam.vl["EPAS3P_sysStatus"] if self.models_raven else cp.vl["EPAS_sysStatus"]
      self.hands_on_level = epas_status["EPAS_handsOnLevel"]
      self.steer_warning = self.can_define.dv["EPAS_sysStatus"]["EPAS_eacErrorCode"].get(int(epas_status["EPAS_eacErrorCode"]), None)
      steer_status = self.can_define.dv["EPAS_sysStatus"]["EPAS_eacStatus"].get(int(epas_status["EPAS_eacStatus"]), None)
      ret.steeringAngleDeg = -epas_status["EPAS_internalSAS"]
      ret.steeringRateDeg = -cp.vl["STW_ANGLHP_STAT"]["StW_AnglHP_Spd"]  # This is from a different angle sensor, and at different rate
      ret.steeringTorque = -epas_status["EPAS_torsionBarTorque"]

    ret.steeringPressed = (self.hands_on_level > 0)
    ret.steerFaultPermanent = steer_status in ["EAC_FAULT"]
    ret.steerFaultTemporary = (self.steer_warning not in ("EAC_ERROR_IDLE", "EAC_ERROR_HANDS_ON"))

    # Cruise state
    cruise_state = self.can_define.dv["DI_state"]["DI_cruiseState"].get(int(cp.vl["DI_state"]["DI_cruiseState"]), None)
    speed_units = self.can_define.dv["DI_state"]["DI_speedUnits"].get(int(cp.vl["DI_state"]["DI_speedUnits"]), None)

    self.acc_enabled = (cruise_state in ("ENABLED", "STANDSTILL", "OVERRIDE", "PRE_FAULT", "PRE_CANCEL"))
    ret.cruiseState.enabled = self.acc_enabled
    if speed_units == "KPH":
      ret.cruiseState.speed = cp.vl["DI_state"]["DI_digitalSpeed"] * CV.KPH_TO_MS
    elif speed_units == "MPH":
      ret.cruiseState.speed = cp.vl["DI_state"]["DI_digitalSpeed"] * CV.MPH_TO_MS
    ret.cruiseState.available = ((cruise_state == "STANDBY") or ret.cruiseState.enabled)
    ret.cruiseState.standstill = False  # This needs to be false, since we can resume from stop without sending anything special

    # Gear
    gear_msg = "DI_systemStatus" if self.model3_y else "DI_torque2"
    ret.gearShifter = GEAR_MAP[self.can_define.dv[gear_msg]["DI_gear"].get(int(cp.vl[gear_msg]["DI_gear"]), "DI_GEAR_INVALID")]

    # Buttons
    button_events = []
    for button in (MODEL3_Y_BUTTONS if self.model3_y else BUTTONS):
      btn_parser = cp_adas if self.model3_y else cp
      state = (btn_parser.vl[button.can_addr][button.can_msg] in button.values)
      if self.button_states[button.event_type] != state:
        event = car.CarState.ButtonEvent.new_message()
        event.type = button.event_type
        event.pressed = state
        button_events.append(event)
      self.button_states[button.event_type] = state
    ret.buttonEvents = button_events

    # Doors
    if self.model3_y:
      ret.doorOpen = any([cp_adas.vl["VCLEFT_doorStatus"]["VCLEFT_frontLatchSwitch"] != 1,
                          cp_adas.vl["VCLEFT_doorStatus"]["VCLEFT_rearLatchSwitch"] != 1,
                          cp_adas.vl["VCRIGHT_doorStatus"]["VCRIGHT_frontLatchSwitch"] != 1,
                          cp_adas.vl["VCRIGHT_doorStatus"]["VCRIGHT_rearLatchSwitch"] != 1,
                          cp_adas.vl["VCRIGHT_doorStatus"]["VCRIGHT_trunkLatchStatus"] != 2])
    else:
      ret.doorOpen = any((self.can_define.dv["GTW_carState"][door].get(int(cp.vl["GTW_carState"][door]), "OPEN") == "OPEN") for door in DOORS)

    # Blinkers
    if self.model3_y:
      # maybe use DAS_turnIndicatorRequestReason
      ret.leftBlinker = (cp_adas.vl["ID3F5VCFRONT_lighting"]["VCFRONT_indicatorLeftRequest"] != 0)
      ret.rightBlinker = (cp_adas.vl["ID3F5VCFRONT_lighting"]["VCFRONT_indicatorRightRequest"] != 0)
    else:
      ret.leftBlinker = (cp.vl["GTW_carState"]["BC_indicatorLStatus"] == 1)
      ret.rightBlinker = (cp.vl["GTW_carState"]["BC_indicatorRStatus"] == 1)

    # Seatbelt
    if self.models_raven:
      ret.seatbeltUnlatched = (cp.vl["DriverSeat"]["buckleStatus"] != 1)
    elif self.model3_y:
      ret.seatbeltUnlatched = cp_adas.vl["VCLEFT_switchStatus"]["VCLEFT_frontBuckleSwitch"] == 1
    else:
      ret.seatbeltUnlatched = (cp.vl["SDM1"]["SDM_bcklDrivStatus"] != 1)

    # TODO: blindspot

    # AEB
    ret.stockAeb = (cp_cam.vl["DAS_control"]["DAS_aebEvent"] == 1)

    # Messages needed by carcontroller
    if self.model3_y:
      self.sccm_right_stalk = copy.copy(cp_adas.vl["SCCM_rightStalk"])
    else:
      self.msg_stw_actn_req = copy.copy(cp.vl["STW_ACTN_RQ"])

    self.das_control = copy.copy(cp_cam.vl["DAS_control"])
    self.das_control_counters.extend(cp_cam.vl_all["DAS_control"]["DAS_controlCounter"])

    return ret

  @staticmethod
  def get_can_parser(CP):
    messages = [
      # sig_address, frequency
      ("ESP_B", 50),
      ("DI_torque1", 100),
      ("DI_torque2", 100),
      ("STW_ANGLHP_STAT", 100),
      ("EPAS_sysStatus", 25),
      ("DI_state", 10),
      ("STW_ACTN_RQ", 10),
      ("GTW_carState", 10),
      ("BrakeMessage", 50),
    ]

    if CP.carFingerprint == CAR.TESLA_MODELS_RAVEN:
      messages.append(("DriverSeat", 20))
    else:
      messages.append(("SDM1", 10))

    if CP.carFingerprint in [CAR.TESLA_AP3_MODEL3, CAR.TESLA_AP3_MODELY]:
      messages = [
        # sig_address, frequency
        ("ESP_B", 50),
        ("DI_systemStatus", 100),
        ("IBST_status", 25),
        ("DI_state", 10),
        ("EPAS3S_sysStatus", 100)
      ]

    return CANParser(DBC[CP.carFingerprint]['chassis'], messages, CANBUS.chassis)

  @staticmethod
  def get_cam_can_parser(CP):
    messages = [
      # sig_address, frequency
      ("DAS_control", 40),
    ]

    if CP.carFingerprint == CAR.TESLA_MODELS_RAVEN:
      messages.append(("EPAS3P_sysStatus", 100))
    elif CP.carFingerprint in [CAR.TESLA_AP3_MODEL3, CAR.TESLA_AP3_MODELY]:
      messages = [
        ("DAS_control", 25),
      ]

    return CANParser(DBC[CP.carFingerprint]['chassis'], messages, CANBUS.autopilot_chassis)

  @staticmethod
  def get_adas_can_parser(CP):  # Vehicle Can on Model 3
    if CP.carFingerprint in [CAR.TESLA_AP3_MODEL3, CAR.TESLA_AP3_MODELY]:
      messages = [
        ("VCLEFT_switchStatus", 20),
        ("SCCM_leftStalk", 10),
        ("SCCM_rightStalk", 10),
        ("SCCM_steeringAngleSensor", 100),
        ("DAS_bodyControls", 2),
        ("ID3F5VCFRONT_lighting", 10),
        ("VCLEFT_doorStatus", 10),
        ("VCRIGHT_doorStatus", 10),
      ]
      return CANParser(DBC[CP.carFingerprint]["pt"], messages, CANBUS.vehicle)
