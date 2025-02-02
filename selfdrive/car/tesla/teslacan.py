import crcmod

from openpilot.common.conversions import Conversions as CV
from openpilot.selfdrive.car.tesla.values import CANBUS, CarControllerParams


class TeslaCAN:
  def __init__(self, packer, pt_packer):
    self.packer = packer
    self.pt_packer = pt_packer
    self.crc = crcmod.mkCrcFun(0x11d, initCrc=0x00, rev=False, xorOut=0xff)

  @staticmethod
  def checksum(msg_id, dat):
    # TODO: get message ID from name instead
    ret = (msg_id & 0xFF) + ((msg_id >> 8) & 0xFF)
    ret += sum(dat)
    return ret & 0xFF

  def create_steering_control(self, angle, enabled, counter):
    values = {
      "DAS_steeringAngleRequest": -angle,
      "DAS_steeringHapticRequest": 0,
      "DAS_steeringControlType": 1 if enabled else 0,
      "DAS_steeringControlCounter": counter,
    }

    data = self.packer.make_can_msg("DAS_steeringControl", CANBUS.chassis, values)[2]
    values["DAS_steeringControlChecksum"] = self.checksum(0x488, data[:3])
    return self.packer.make_can_msg("DAS_steeringControl", CANBUS.chassis, values)

  def create_action_request(self, msg_stw_actn_req, cancel, bus, counter):
    # We copy this whole message when spamming cancel
    values = {s: msg_stw_actn_req[s] for s in [
      "SpdCtrlLvr_Stat",
      "VSL_Enbl_Rq",
      "SpdCtrlLvrStat_Inv",
      "DTR_Dist_Rq",
      "TurnIndLvr_Stat",
      "HiBmLvr_Stat",
      "WprWashSw_Psd",
      "WprWash_R_Sw_Posn_V2",
      "StW_Lvr_Stat",
      "StW_Cond_Flt",
      "StW_Cond_Psd",
      "HrnSw_Psd",
      "StW_Sw00_Psd",
      "StW_Sw01_Psd",
      "StW_Sw02_Psd",
      "StW_Sw03_Psd",
      "StW_Sw04_Psd",
      "StW_Sw05_Psd",
      "StW_Sw06_Psd",
      "StW_Sw07_Psd",
      "StW_Sw08_Psd",
      "StW_Sw09_Psd",
      "StW_Sw10_Psd",
      "StW_Sw11_Psd",
      "StW_Sw12_Psd",
      "StW_Sw13_Psd",
      "StW_Sw14_Psd",
      "StW_Sw15_Psd",
      "WprSw6Posn",
      "MC_STW_ACTN_RQ",
      "CRC_STW_ACTN_RQ",
    ]}

    if cancel:
      values["SpdCtrlLvr_Stat"] = 1
      values["MC_STW_ACTN_RQ"] = counter

    data = self.packer.make_can_msg("STW_ACTN_RQ", bus, values)[2]
    values["CRC_STW_ACTN_RQ"] = self.crc(data[:7])
    return self.packer.make_can_msg("STW_ACTN_RQ", bus, values)

  def create_longitudinal_commands(self, acc_state, speed, min_accel, max_accel, cnt, buses):
    messages = []
    values = {
      "DAS_setSpeed": speed * CV.MS_TO_KPH,
      "DAS_accState": acc_state,
      "DAS_aebEvent": 0,
      "DAS_jerkMin": CarControllerParams.JERK_LIMIT_MIN,
      "DAS_jerkMax": CarControllerParams.JERK_LIMIT_MAX,
      "DAS_accelMin": min_accel,
      "DAS_accelMax": max_accel,
      "DAS_controlCounter": cnt,
      "DAS_controlChecksum": 0,
    }

    for packer, bus in buses:
      data = packer.make_can_msg("DAS_control", bus, values)[2]
      values["DAS_controlChecksum"] = self.checksum(0x2b9, data[:7])
      messages.append(packer.make_can_msg("DAS_control", bus, values))
    return messages

  def model3_cancel_acc(self, counter, position):
    # TODO: Implement CRC checksum instead of lookup table.
    if position == 1:  # half up
      crc_lookup = [166, 164, 178, 141, 163, 161, 61, 25, 172, 69, 22, 108, 169, 207, 209, 219]
    else:  # neutral position
      position = 0
      crc_lookup = [70, 68, 82, 109, 67, 65, 221, 249, 76, 165, 246, 140, 73, 47, 49, 59]

    values = {"SCCM_rightStalkCounter": counter,
              "SCCM_rightStalkCrc": crc_lookup[counter],
              "SCCM_rightStalkReserved1": 0,
              "SCCM_parkButtonStatus": 0,
              "SCCM_rightStalkReserved2": 0,
              "SCCM_rightStalkStatus": position,
              }

    return self.pt_packer.make_can_msg("SCCM_rightStalk", CANBUS.radar, values)
