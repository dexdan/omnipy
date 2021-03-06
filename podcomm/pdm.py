from .pdmutils import *
from .nonce import *
from .radio import Radio
from .message import Message, MessageType
from .exceptions import PdmError, OmnipyError, TransmissionOutOfSyncError
from .definitions import *

from decimal import *
import time
import struct
from datetime import datetime, timedelta

class Pdm:
    def __init__(self, pod):
        self.nonce = Nonce(pod.lot, pod.tid, seekNonce=pod.lastNonce, seed=pod.nonceSeed)
        self.pod = pod
        self.radio = Radio(pod.msgSequence, pod.packetSequence)
        self.logger = getLogger()

    def updatePodStatus(self, update_type=0):
        try:
            self._assert_pod_address_assigned()
            if update_type == 0 and \
                    self.pod.lastUpdated is not None and \
                    time.time() - self.pod.lastUpdated < 60:
                return
            with pdmlock():
                self.logger.debug("updating pod status")
                self._update_status(update_type, stay_connected=False)

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()
            self._savePod()

    def acknowledge_alerts(self, alert_mask):
        try:
            self._assert_can_acknowledge_alerts()

            with pdmlock():
                self.logger.debug("acknowledging alerts with bitmask %d" % alert_mask)
                self._acknowledge_alerts(alert_mask)

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()
            self._savePod()

    def is_busy(self):
        try:
            with pdmlock():
                return self._is_bolus_running()
        except PdmBusyError:
            return True
        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()

    # def clear_alert(self, alert_bit):
    #     try:
    #         self._assert_can_acknowledge_alerts()
    #
    #         with pdmlock():
    #             self.logger.debug("clearing alert %d" % alert_bit)
    #             self._configure_alert(alert_bit, clear=True)
    #     except PdmError:
    #         raise
    #     except OmnipyError as oe:
    #         raise PdmError("Command failed") from oe
    #     except Exception as e:
    #         raise PdmError("Unexpected error") from e
    #     finally:
    #         self._savePod()

    def bolus(self, bolus_amount, beep=False):
        try:
            with pdmlock():
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_immediate_bolus_not_active()
                self._assert_not_faulted()
                self._assert_status_running()

                if bolus_amount > self.pod.maximumBolus:
                    raise PdmError("Bolus exceeds defined maximum bolus of %.2fU" % self.pod.maximumBolus)

                pulseCount = int(bolus_amount * Decimal(20))

                if pulseCount == 0:
                    raise PdmError("Cannot do a zero bolus")

                pulseSpan = pulseCount * 16
                if pulseSpan > 0x3840:
                    raise PdmError("Bolus would exceed the maximum time allowed for an immediate bolus")

                if self._is_bolus_running():
                    raise PdmError("A previous bolus is already running")

                if bolus_amount > self.pod.reservoir:
                    raise PdmError("Cannot bolus %.2f units, reservoir capacity is at: %.2f")

                commandBody = struct.pack(">I", 0)
                commandBody += b"\x02"

                bodyForChecksum = b"\x01"
                bodyForChecksum += struct.pack(">H", pulseSpan)
                bodyForChecksum += struct.pack(">H", pulseCount)
                bodyForChecksum += struct.pack(">H", pulseCount)
                checksum = getChecksum(bodyForChecksum)

                commandBody += struct.pack(">H", checksum)
                commandBody += bodyForChecksum

                msg = self._createMessage(0x1a, commandBody)


                reminders = 0
                if beep:
                    reminders |= 0x40

                deliveryStart = 200000

                commandBody = bytes([reminders])
                commandBody += struct.pack(">H", pulseCount * 10)
                commandBody += struct.pack(">I", deliveryStart)
                commandBody += b"\x00\x00\x00\x00\x00\x00"
                msg.addCommand(0x17, commandBody)

                self._sendMessage(msg, with_nonce=True, request_msg="BOLUS %02.2f" % float(bolus_amount))

                if self.pod.bolusState != BolusState.Immediate:
                    raise PdmError("Pod did not confirm bolus")

                self.pod.last_enacted_bolus_start = time.time()
                self.pod.last_enacted_bolus_amount = float(bolus_amount)

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()
            self._savePod()


    def cancelBolus(self, beep=False):
        try:
            with pdmlock():
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_not_faulted()
                self._assert_status_running()

                if self._is_bolus_running():
                    self.logger.debug("Canceling running bolus")
                    self._cancelActivity(cancelBolus=True, beep=beep)
                    if self.pod.bolusState == BolusState.Immediate:
                        raise PdmError("Failed to cancel bolus")
                    else:
                        self.pod.last_enacted_bolus_amount = float(-1)
                        self.pod.last_enacted_bolus_start = time.time()
                else:
                    raise PdmError("Bolus is not running")

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()
            self._savePod()

    def cancelTempBasal(self, beep=False):
        try:
            with pdmlock():
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_immediate_bolus_not_active()
                self._assert_not_faulted()
                self._assert_status_running()

                if self._is_temp_basal_active():
                    self.logger.debug("Canceling temp basal")
                    self._cancelActivity(cancelTempBasal=True, beep=beep)
                    if self.pod.basalState == BasalState.TempBasal:
                        raise PdmError("Failed to cancel temp basal")
                    else:
                        self.pod.last_enacted_temp_basal_duration = float(-1)
                        self.pod.last_enacted_temp_basal_start = time.time()
                        self.pod.last_enacted_temp_basal_amount = float(-1)
                else:
                    self.logger.warning("Cancel temp basal received, while temp basal was not active. Ignoring.")

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()
            self._savePod()

    def setTempBasal(self, basalRate, hours, confidenceReminder=False):
        try:
            with pdmlock():
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_immediate_bolus_not_active()
                self._assert_not_faulted()
                self._assert_status_running()

                halfHours = int(hours * Decimal(2))

                if halfHours > 24 or halfHours < 1:
                    raise PdmError("Requested duration is not valid")

                if self.pod is None or not self.pod.is_active():
                    raise PdmError("Pod not active")
                if basalRate > Decimal(self.pod.maximumTempBasal):
                    raise PdmError("Requested rate exceeds maximum temp basal setting")
                if basalRate > Decimal(30):
                    raise PdmError("Requested rate exceeds maximum temp basal capability")

                if self._is_temp_basal_active():
                    self.cancelTempBasal()

                halfHourUnits = [basalRate / Decimal(2)] * halfHours
                pulseList = getPulsesForHalfHours(halfHourUnits)
                iseList = getInsulinScheduleTableFromPulses(pulseList)

                iseBody = getStringBodyFromTable(iseList)
                pulseBody = getStringBodyFromTable(pulseList)

                commandBody = struct.pack(">I", 0)
                commandBody += b"\x01"

                bodyForChecksum = bytes([halfHours])
                bodyForChecksum += struct.pack(">H", 0x3840)
                bodyForChecksum += struct.pack(">H", pulseList[0])
                checksum = getChecksum(bodyForChecksum + pulseBody)

                commandBody += struct.pack(">H", checksum)
                commandBody += bodyForChecksum
                commandBody += iseBody

                msg = self._createMessage(0x1a, commandBody)

                reminders = 0
                if confidenceReminder:
                    reminders |= 0x40

                commandBody = bytes([reminders])
                commandBody += b"\x00"

                pulseEntries = getPulseIntervalEntries(halfHourUnits)

                firstPulseCount, firstInterval = pulseEntries[0]
                commandBody += struct.pack(">H", firstPulseCount)
                commandBody += struct.pack(">I", firstInterval)

                for pulseCount, interval in pulseEntries:
                    commandBody += struct.pack(">H", pulseCount)
                    commandBody += struct.pack(">I", interval)

                msg.addCommand(0x16, commandBody)

                self._sendMessage(msg, with_nonce=True, request_msg="TEMPBASAL %02.2fU/h %02.1fh" % (float(basalRate),
                                                                                                 float(hours)))

                if self.pod.basalState != BasalState.TempBasal:
                    raise PdmError("Failed to set temp basal")
                else:
                    self.pod.last_enacted_temp_basal_duration = float(hours)
                    self.pod.last_enacted_temp_basal_start = time.time()
                    self.pod.last_enacted_temp_basal_amount = float(basalRate)

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()
            self._savePod()

    def set_basal_schedule(self, schedule):
        try:
            with pdmlock():
                self._assert_pod_address_assigned()
                self._assert_can_generate_nonce()
                self._assert_immediate_bolus_not_active()
                self._assert_not_faulted()
                self._assert_status_running()

                if self._is_temp_basal_active():
                    raise PdmError("Cannot change basal schedule while a temp. basal is active")

                if len(schedule) != 48:
                    raise PdmError("A full schedule of 48 half hours is needed to change basal program")

                min_rate = Decimal("0.05")
                max_rate = Decimal("30")

                for entry in schedule:
                    if entry < min_rate:
                        raise PdmError("A basal rate schedule entry cannot be less than 0.05U")
                    if entry > max_rate:
                        raise PdmError("A basal rate schedule entry cannot be more than 30U")

                commandBody = struct.pack(">I", 0)
                commandBody += b"\x00"

                utcOffset = timedelta(minutes=self.pod.utcOffset)
                podDate = datetime.utcnow() + utcOffset

                hour = podDate.hour
                minute = podDate.minute
                second = podDate.second

                currentHalfHour = hour * 2
                secondsUntilHalfHour = 0
                if minute < 30:
                    secondsUntilHalfHour += (30 - minute - 1) * 60
                else:
                    secondsUntilHalfHour += (60 - minute - 1) * 60
                    currentHalfHour += 1

                secondsUntilHalfHour += (60 - second)

                pulseTable = getPulsesForHalfHours(schedule)
                pulsesRemainingCurrentHour = int(secondsUntilHalfHour * pulseTable[currentHalfHour] / 1800)
                iseBody = getStringBodyFromTable(getInsulinScheduleTableFromPulses(pulseTable))

                bodyForChecksum = bytes([currentHalfHour])
                bodyForChecksum += struct.pack(">H", secondsUntilHalfHour * 8)
                bodyForChecksum += struct.pack(">H", pulsesRemainingCurrentHour)
                getChecksum(bodyForChecksum + getStringBodyFromTable(pulseTable))

                commandBody += bodyForChecksum + iseBody

                msg = self._createMessage(0x1a, commandBody)

                reminders = 0
                # if confidenceReminder:
                #     reminders |= 0x40

                commandBody = bytes([reminders])

                commandBody += b"\x00"
                pulseEntries = getPulseIntervalEntries(schedule)

                commandBody += struct.pack(">H", pulsesRemainingCurrentHour*10)
                commandBody += struct.pack(">I", int(secondsUntilHalfHour * 1000 * 1000 / pulsesRemainingCurrentHour))

                for pulseCount, interval in pulseEntries:
                    commandBody += struct.pack(">H", pulseCount)
                    commandBody += struct.pack(">I", interval)

                msg.addCommand(0x13, commandBody)

                self._sendMessage(msg, with_nonce=True, request_msg="SETBASALSCHEDULE %s" % schedule)

                if self.pod.basalState != BasalState.Program:
                    raise PdmError("Failed to set basal schedule")
                else:
                    self.pod.basalSchedule = schedule

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()
            self._savePod()


    def deactivate_pod(self):
        try:
            with pdmlock():
                msg = self._createMessage(0x1c, bytes([0, 0, 0, 0]))
                self._sendMessage(msg, with_nonce=True, request_msg="DEACTIVATE POD")

        except OmnipyError:
            raise
        except Exception as e:
            raise PdmError("Unexpected error") from e
        finally:
            self.radio.disconnect()
            self._savePod()

    def _cancelActivity(self, cancelBasal=False, cancelBolus=False, cancelTempBasal=False, beep=False):
        self.logger.debug("Running cancel activity for basal: %s - bolus: %s - tempBasal: %s" % (
        cancelBasal, cancelBolus, cancelTempBasal))
        commandBody = struct.pack(">I", 0)
        if beep:
            c = 0x60
        else:
            c = 0

        act_str = ""
        if cancelBolus:
            c = c | 0x04
            act_str += "BOLUS "
        if cancelTempBasal:
            c = c | 0x02
            act_str += "TEMPBASAL "
        if cancelBasal:
            c = c | 0x01
            act_str += "BASAL "
        commandBody += bytes([c])

        msg = self._createMessage(0x1f, commandBody)
        self._sendMessage(msg, with_nonce=True, stay_connected=True, request_msg="CANCEL %s" % act_str)

    def _createMessage(self, commandType, commandBody):
        msg = Message(MessageType.PDM, self.pod.address, sequence=self.radio.messageSequence)
        msg.addCommand(commandType, commandBody)
        return msg

    def _savePod(self):
        try:
            self.logger.debug("Saving pod status")
            self.pod.msgSequence = self.radio.messageSequence
            self.pod.packetSequence = self.radio.packetSequence
            self.pod.lastNonce = self.nonce.lastNonce
            self.pod.nonceSeed = self.nonce.seed
            self.pod.Save()
            self.logger.debug("Saved pod status")
        except Exception as e:
            raise PdmError("Pod status was not saved") from e

    def _sendMessage(self, message, with_nonce=False, nonce_retry_count=0, stay_connected=False, request_msg=None,
                     resync_allowed=True):
        requested_stay_connected = stay_connected
        if with_nonce:
            nonce = self.nonce.getNext()
            if nonce == FAKE_NONCE:
                stay_connected = True
            message.setNonce(nonce)
        try:
            response_message = self.radio.send_request_get_response(message, stay_connected=stay_connected)
        except TransmissionOutOfSyncError:
            if resync_allowed:
                self._interim_resync()
                return self._sendMessage(message, with_nonce=with_nonce, nonce_retry_count=nonce_retry_count,
                                         stay_connected=requested_stay_connected, request_msg=request_msg,
                                         resync_allowed=False)
            else:
                raise

        contents = response_message.getContents()
        for (ctype, content) in contents:
            # if ctype == 0x01:  # pod info response
            #     self.pod.setupPod(content)
            if ctype == 0x1d:  # status response
                self.pod.handle_status_response(content, original_request=request_msg)
            elif ctype == 0x02:  # pod faulted or information
                self.pod.handle_information_response(content, original_request=request_msg)
            elif ctype == 0x06:
                if content[0] == 0x14:  # bad nonce error
                    if nonce_retry_count == 0:
                        self.logger.debug("Bad nonce error - renegotiating")
                    elif nonce_retry_count > 3:
                        raise PdmError("Nonce re-negotiation failed")
                    nonce_sync_word = struct.unpack(">H", content[1:])[0]
                    self.nonce.sync(nonce_sync_word, message.sequence)
                    self.radio.messageSequence = message.sequence
                    return self._sendMessage(message, with_nonce=True, nonce_retry_count=nonce_retry_count + 1,
                                             stay_connected=requested_stay_connected, request_msg=request_msg)

    def _interim_resync(self):
        time.sleep(15)
        commandType = 0x0e
        commandBody = bytes([0])
        msg = self._createMessage(commandType, commandBody)
        self._sendMessage(msg, stay_connected=True, request_msg="STATUS REQ %d" % 0,
                          resync_allowed=True)
        time.sleep(5)

    def _update_status(self, update_type=0, stay_connected=True):
        commandType = 0x0e
        commandBody = bytes([update_type])
        msg = self._createMessage(commandType, commandBody)
        self._sendMessage(msg, stay_connected=stay_connected, request_msg="STATUS REQ %d" % update_type)

    def _acknowledge_alerts(self, alert_mask):
        commandType = 0x11
        commandBody = bytes([0, 0, 0, 0, alert_mask])
        msg = self._createMessage(commandType, commandBody)
        self._sendMessage(msg, with_nonce=True, stay_connected=True, request_msg="ACK 0x%2X " % alert_mask)

    # def _configure_alerts(self, alerts):
    #     commandType = 0x19
    #     commandBody = bytes([0, 0, 0, 0])
    #
    #     for alert in alerts:
    #         commandBody += self._configure_alert(alert)
    #
    #     msg = self._createMessage(commandType, commandBody)
    #     self._sendMessage(msg, with_nonce=True)

    # def _configure_alert(self, alert_bit, activate, trigger_reservoir, trigger_auto_off,
    #                      duration_minutes, alert_after_minutes, alert_after_reservoir,
    #                      beep_repeat_type, beep_type):
    #
    #     if alert_after_minutes is None:
    #         if alert_after_reservoir is None:
    #             raise PdmError("Either alert_after_minutes or alert_after_reservoir must be set")
    #         elif not trigger_reservoir:
    #             raise PdmError("Trigger reservoir must be True if alert_after_reservoir is to be set")
    #     else:
    #         if alert_after_reservoir is not None:
    #             raise PdmError("Only one of alert_after_minutes or alert_after_reservoir must be set")
    #         elif trigger_reservoir:
    #             raise PdmError("Trigger reservoir must be False if alert_after_minutes is to be set")
    #
    #     if duration_minutes > 0x1FF:
    #         raise PdmError("Alert duration in minutes cannot be more than %d", 0x1ff)
    #     elif duration_minutes < 0:
    #         raise PdmError("Invalid alert duration value")
    #
    #     if alert_after_minutes is not None and alert_after_minutes > 4800:
    #         raise PdmError("Alert cannot be set beyond 80 hours")
    #     if alert_after_minutes is not None and alert_after_minutes < 0:
    #         raise PdmError("Invalid value for alert_after_minutes")
    #
    #     if alert_after_reservoir is not None and alert_after_reservoir > 50:
    #         raise PdmError("Alert cannot be set for more than 50 units")
    #     if alert_after_reservoir is not None and alert_after_minutes < 0:
    #         raise PdmError("Invalid value for alert_after_reservoir")
    #
    #     b0 = alert_bit << 4
    #     if activate:
    #         b0 |= 0x08
    #     if trigger_reservoir:
    #         b0 |= 0x04
    #     if trigger_auto_off:
    #         b0 |= 0x02
    #
    #     b0 |= (duration_minutes >> 8) & 0x0001
    #     b1 = duration_minutes & 0x00ff
    #
    #     if alert_after_minutes is not None:
    #         b2 = alert_after_minutes >> 8
    #         b3 = alert_after_minutes & 0x00ff
    #     else:
    #         reservoir_limit = int(alert_after_reservoir * 10)
    #         b2 = reservoir_limit >> 8
    #         b3 = reservoir_limit & 0x00ff
    #
    #     return bytes([b0, b1, b2, b3, beep_repeat_type, beep_type])

    def _is_bolus_running(self):
        if self.pod.lastUpdated is not None and self.pod.bolusState != BolusState.Immediate:
            return False

        if self.pod.last_enacted_bolus_amount is not None \
                and self.pod.last_enacted_bolus_start is not None:

            if self.pod.last_enacted_bolus_amount < 0:
                return False

            now = time.time()
            bolus_end_earliest = (self.pod.last_enacted_bolus_amount * 35) + self.pod.last_enacted_bolus_start
            bolus_end_latest = (self.pod.last_enacted_bolus_amount * 45) + 10 + self.pod.last_enacted_bolus_start
            if now > bolus_end_latest:
                return False
            elif now < bolus_end_earliest:
                return True

        self._update_status()
        return self.pod.bolusState == BolusState.Immediate

    def _is_basal_schedule_active(self):
        if self.pod.lastUpdated is not None and self.pod.basalState == BasalState.NotRunning:
            return False

        self._update_status()
        return self.pod.basalState == BasalState.Program

    def _is_temp_basal_active(self):
        if self.pod.lastUpdated is not None and self.pod.basalState != BasalState.TempBasal:
            return False

        if self.pod.last_enacted_temp_basal_start is not None \
                and self.pod.last_enacted_temp_basal_duration is not None:
            if self.pod.last_enacted_temp_basal_amount < 0:
                return False
            now = time.time()
            temp_basal_end_earliest = self.pod.last_enacted_temp_basal_start + \
                                      (self.pod.last_enacted_temp_basal_duration * 3600) - 60
            temp_basal_end_latest = self.pod.last_enacted_temp_basal_start + \
                                      (self.pod.last_enacted_temp_basal_duration * 3660) + 60
            if now > temp_basal_end_latest:
                return False
            elif now < temp_basal_end_earliest:
                return True

        self._update_status()
        return self.pod.basalState == BasalState.TempBasal

    def _assert_pod_address_assigned(self):
        if self.pod is None:
            raise PdmError("No pod assigned")

        if self.pod.address is None:
            raise PdmError("Radio address unknown")

    def _assert_can_deactivate(self):
        self._assert_pod_address_assigned()
        self._assert_can_generate_nonce()
        if self.pod.progress < PodProgress.PairingSuccess:
            raise PdmError("Pod is not paired")
        if self.pod.progress > PodProgress.AlertExpiredShuttingDown:
            raise PdmError("Pod already deactivated")

    def _assert_can_acknowledge_alerts(self):
        self._assert_pod_address_assigned()
        if self.pod.progress < PodProgress.PairingSuccess:
            raise PdmError("Pod not paired completely yet.")

        if self.pod.progress == PodProgress.ErrorShuttingDown:
            raise PdmError("Pod is shutting down, cannot acknowledge alerts.")

        if self.pod.progress == PodProgress.AlertExpiredShuttingDown:
            raise PdmError("Acknowledgement period expired, pod is shutting down")

        if self.pod.progress > PodProgress.AlertExpiredShuttingDown:
            raise PdmError("Pod is not active")

    def _assert_can_generate_nonce(self):
        if self.pod.lot is None:
            raise PdmError("Lot number is not defined")

        if self.pod.tid is None:
            raise PdmError("Pod serial number is not defined")

    def _assert_status_running(self):
        if self.pod.progress < PodProgress.Running:
            raise PdmError("Pod is not yet running")

        if self.pod.progress > PodProgress.RunningLow:
            raise PdmError("Pod has stopped")

    def _assert_not_faulted(self):
        if self.pod.faulted:
            raise PdmError("Pod is faulted")

    def _assert_no_active_alerts(self):
        if self.pod.alert_states != 0:
            raise PdmError("Pod has active alerts")

    def _assert_immediate_bolus_not_active(self):
        if self._is_bolus_running():
            raise PdmError("Pod is busy delivering a bolus")


