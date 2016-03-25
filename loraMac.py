from multiprocessing import Queue
import threading
import base64
from CryptoPlus.Cipher import python_AES # PyCryptoPlus
import random
import logging
import struct
import json
from collections import deque

DOWNLINK_QUEUE_MAX_SIZE = 32

class LoRaMacCrypto:
    CRYPTO_BLOCK_SIZE = 16

    def __init__(self, appKeyStr):
        self.appKeyStr = appKeyStr
        self.aesWithNwkSKey = None
        self.aesWithAppSKey = None

    def setSessionKeys(self, nwkSKeyStr, appSKeyStr):
        # TODO: create AES objects that can be reused. But remember to reset
        # them after each encryption operation
        pass

    def padToBlockSize(self, byteStr):
        # zero padding
        if len(byteStr) % self.CRYPTO_BLOCK_SIZE != 0:
            buf = byteStr + str(bytearray([0]* \
                                (self.CRYPTO_BLOCK_SIZE-len(byteStr))))
        else:
            buf = byteStr

        assert(len(buf) % self.CRYPTO_BLOCK_SIZE == 0)
        return buf

    def computeJoinMic(self, byteStr):
        '''
        byteStr is everything in the PHYPayload except MIC
        secret key is AppKey

        LoRaWAN Specification v1.0 Ch6.2.4 and Ch6.2.5
        '''
        # no padding is needed
        cmacWithAppKey = python_AES.new(self.appKeyStr, python_AES.MODE_CMAC)
        return cmacWithAppKey.encrypt(byteStr)[:4]

    def encryptJoinAccept(self, byteStr):
        '''
        byteStr is | AppNonce | NetID | DevAddr | RFU | RxDelay | CFList | MIC |
        secret key is AppKey

        LoRaWAN Specification v1.0 Ch6.2.5
        '''
        paddedBuf = self.padToBlockSize(byteStr)
        aesWithAppKey = python_AES.new(self.appKeyStr, python_AES.MODE_ECB)
        return aesWithAppKey.decrypt(paddedBuf) # DECRYPT here is on purpose

    def deriveSessionKey(self, byteStr):
        '''
        byteStr is | 0x01 or 0x02 | AppNonce | NetID | DevNonce | padding |
        secret key is AppKey

        LoRaWAN Specification v1.0 Ch6.2.5
        '''
        # Just to be certain that the buffer is padded
        paddedBuf = self.padToBlockSize(byteStr)
        aesWithAppKey = python_AES.new(self.appKeyStr, python_AES.MODE_ECB)
        return aesWithAppKey.encrypt(paddedBuf)

class LoRaEndDevice:
    def __init__(self, appEUI, devEUI, appKeyStr):
        '''
        appEUI: application unique identifier as a 64-bit integer (big endian)
        devEUI: device unique identifier as a 64-bit integer (big endian)
        appKeyStr: 16-byte encryption secret key as a byte string
        '''
        ### RF parameters
        self.dlModulation = "LORA"
        self.dlIpol = True # LoRaWAN recommends downlink to use inverted pol
        self.dlNumPreamble = 8
        self.rx2FreqMHz = 923.300000
        self.rx2Datarate = "SF10BW500"
        self.rx2Codingrate = "4/5"
        self.receiveDelay1_usec = 1000000
        self.joinAcceptDelay1_usec = 5000000
        self.joinAcceptDelay2_usec = 6000000

        ### internal variables
        self.crypto = LoRaMacCrypto(appKeyStr)
        self.devAddr = None
        self.appEUI = appEUI
        self.devEUI = devEUI
        self.appKeyStr = appKeyStr
        self.nwkSKeyStr = '' # will be set in LoRaMac.handleJoinRequest()
        self.appSKeyStr = '' # will be set in LoRaMac.handleJoinRequest()
        self.joined = False
        self.gateways = set()
        self.dlQueue = deque(maxlen=DOWNLINK_QUEUE_MAX_SIZE)
        self.lock = threading.RLock()

        self.logger = logging.getLogger("Dev(%x)"%devEUI)

    #def lock(self):
    #    self.lock.acquire()

    #def unlock(self):
    #    self.lock.release()

    def setSessionKeys(self, nwkSKeyStr, appSKeyStr):
        self.nwkSKeyStr = nwkSKeyStr
        self.appSKeyStr = appSKeyStr
        self.crypto.setSessionKeys(nwkSKeyStr, appSKeyStr)

    def getRxWindowDelayUsec(self, rxWindow):
        if rxWindow == RX_WINDOW_1:
            return self.receiveDelay1_usec
        elif rxWindow == RX_WINDOW_2:
            # second RX window opens 1 sec after the first one
            return self.receiveDelay1_usec + 1000000
        elif rxWindow == JOIN_ACCEPT_WINDOW_1:
            return self.joinAcceptDelay1_usec
        elif rxWindow == JOIN_ACCEPT_WINDOW_2:
            return self.joinAcceptDelay2_usec
        else:
            self.logger.warn("Unexpected rxWindow parameter %d"%rxWindow)
            return self.receiveDelay1_usec

    def putDownlinkMsg(self, msg):
        with self.lock:
            self.dlQueue.append(msg)

    def hasPendingDownlink(self):
        return len(self.dlQueue) != 0

    def popDownlinkMsg(self):
        with self.lock:
            return self.dlQueue.popleft()

    def bindWithGateway(self, gatewayMacAddr, rssi):
        self.gateways.add(gatewayMacAddr)

    def getGatewayForDownlink(self):
        # for now, just arbitrarilly pick one gateway
        return next(iter(self.gateways))

RX_WINDOW_1 = 1
RX_WINDOW_2 = 2
JOIN_ACCEPT_WINDOW_1 = 3
JOIN_ACCEPT_WINDOW_2 = 4

MTYPE_JOIN_REQUEST = 0
MTYPE_JOIN_ACCEPT = 1
MTYPE_UNCONFIRMED_DATA_UP = 2
MTYPE_UNCONFIRMED_DATA_DOWN = 3
MTYPE_CONFIRMED_DATA_UP = 4
MTYPE_CONFIRMED_DATA_DOWN = 5
MTYPE_RFU = 6
MTYPE_PROPRIETARY = 7

MAJOR_VERSION_LORAWAN = 0

class LoRaMac:
    ### US902-928 Channel Frequencies
    UPSTREAM_BW125_LOWEST_FREQ_MHZ = 902.3
    UPSTREAM_BW125_SPACING_MHZ = 0.2
    UPSTREAM_BW125_NUM_CHAN = 64
    UPSTREAM_BW500_LOWEST_FREQ_MHZ = 903.0
    UPSTREAM_BW500_SPACING_MHZ = 1.6
    UPSTREAM_BW500_NUM_CHAN = 8
    DOWNSTREAM_BW500_LOWEST_FREQ_MHZ = 923.3
    DOWNSTREAM_BW500_SPACING_MHZ = 0.6
    DOWNSTREAM_BW500_NUM_CHAN = 8

    def __init__(self, networkID, sendToGatewayFn=None):
        self.networkID = networkID & 0x7F # 7-bit
        self.netID = self.networkID # 24-bit
        self.sendToGateway = sendToGatewayFn
        self.euiToDevMap = {}
        self.addrToDevMap = {}

        self.logger = logging.getLogger("LoRaMac")
        self.logger.setLevel(logging.INFO)

    def setGatewaySenderFn(self, fn):
        self.sendToGateway = fn

    def registerEndDevice(self, appEUI, devEUI, appKey):
        '''
        appEUI: application unique identifier. 64-bit integer or an Int list
                of length 8 (little endian)
        devEUI: device unique identifier. 64-bit integer or an Int list
                of length 8 (little endian)
        appKey: 16-byte encryption secret key as a byte string or an Int list
                of length 16.
        '''
        if type(appEUI) != int:
            if type(appEUI) == list and len(appEUI) == 8:
                appEUI_int = struct.unpack(">Q",bytearray(appEUI))[0]
            else:
                raise Exception("EUI must be an integer or an int list with " \
                                "length 8 (big endian).")
        else:
            appEUI_int = appEUI

        if type(devEUI) != int:
            if type(devEUI) == list and len(devEUI) == 8:
                devEUI_int = struct.unpack(">Q",bytearray(devEUI))[0]
            else:
                raise Exception("EUI must be an integer or an int list with " \
                                "length 8 (big endian).")
        else:
            devEUI_int = devEUI

        if type(appKey) != str:
            if type(appKey) == list and len(appKey) == 16:
                appKeyStr = str(bytearray(appKey))
            else:
                raise Exception("AppKey must be a byte array or an int list " \
                                "with length 16.")
        else:
            appKeyStr = appKey

        self.euiToDevMap[(appEUI_int, devEUI_int)] = \
                                LoRaEndDevice(appEUI_int, devEUI_int, appKeyStr)

    def getDevFromEUI(self, appEUI, devEUI):
        if (appEUI, devEUI) in self.euiToDevMap:
            return self.euiToDevMap[(appEUI, devEUI)]
        else:
            return None

    def onGatewayOnline(self, macAddr):
        '''
        Callback to be used by the connection layer/module. Called when a gateway
        makes a connection to the server.
        '''
        self.logger.info("Gateway %x online"%macAddr)
        pass

    def onGatewayOffline(self, macAddr):
        '''
        Callback to be used by the connection layer/module. Called when a gateway
        disconnects from the server.
        '''
        self.logger.info("Gateway %x offline"%macAddr)
        pass

    def getUplinkChannelFromFreq(self, ulDatarate, ulFreqMHz):
        if "500" in ulDatarate:
            # BW500 channels
            return round((ulFreqMHz - self.UPSTREAM_BW500_LOWEST_FREQ_MHZ) / \
                         self.UPSTREAM_BW500_SPACING_MHZ) % \
                   self.UPSTREAM_BW500_NUM_CHAN
        else:
            # BW125 channels
            return round((ulFreqMHz - self.UPSTREAM_BW125_LOWEST_FREQ_MHZ) / \
                         self.UPSTREAM_BW125_SPACING_MHZ) % \
                   self.UPSTREAM_BW125_NUM_CHAN

    def getRxWindow1Freq(self, ulChannel):
        return self.DOWNSTREAM_BW500_LOWEST_FREQ_MHZ + \
               (ulChannel % self.DOWNSTREAM_BW500_NUM_CHAN) * \
               self.DOWNSTREAM_BW500_SPACING_MHZ

    def getRxWindow1DataRate(self, ulDatarate):
        # [TODO]: Take RX1DROffsest into account.
        # Right now just hard code
        assert(ulDatarate == 'SF10BW125') # uplink is DR0
        return 'SF10BW500' # downlink is DR10

    def doDownlinkToDev(self, dev, eouTimestamp, ulChannel, ulDatarate,
                        ulCodingrate):
        # make the following ops atomic
        with dev.lock:
            if not dev.hasPendingDownlink():
                # nothing to do
                self.logger.info("[doDownlinkToDev] No queued downlink")
                return 0
            dlMsg = dev.popDownlinkMsg()

            ## Find out the time for the RX window
            delayUsec = dev.getRxWindowDelayUsec(dlMsg.rxWindow)
            dlTimestamp = eouTimestamp + int(delayUsec)

            ## Prepare the JSON payload
            jsonDict = {}
            # Receive window specific settings
            if (dlMsg.rxWindow == RX_WINDOW_1 or
                dlMsg.rxWindow == JOIN_ACCEPT_WINDOW_1):
                jsonDict["freq"] = self.getRxWindow1Freq(ulChannel)
                jsonDict["datr"] = self.getRxWindow1DataRate(ulDatarate)
                jsonDict["codr"] = ulCodingrate
            else:
                jsonDict["freq"] = dev.rx2FreqMHz
                jsonDict["datr"] = dev.rx2Datarate
                jsonDict["codr"] = dev.rx2Codingrate
            # Settings not specific to receiving window
            jsonDict["tmst"] = dlTimestamp
            jsonDict["rfch"] = 0 # TODO: get this from the gateway object
            jsonDict["powe"] = 20 # TODO: magic number
            jsonDict["modu"] = dev.dlModulation
            jsonDict["ipol"] = dev.dlIpol
            #jsonDict["prea"] = dev.dlNumPreamble
            jsonDict["size"] = dlMsg.payloadSize
            jsonDict["data"] = dlMsg.payloadBase64
            payloadToGw = json.dumps({"txpk":jsonDict}, separators=(',',':'))

        # Send the JSON payload to the corresponding gateway
        gwMacAddr = dev.getGatewayForDownlink()
        self.logger.info("[doDownlinkToDev] Downlink to dev %x via gateway %x" \
                         " with RF params tmst:%d freq:%f datr:%s codr:%s " \
                         "plsize:%d"%(dev.devAddr, gwMacAddr, jsonDict["tmst"],\
                                      jsonDict["freq"], jsonDict["datr"], \
                                      jsonDict["codr"], jsonDict["size"]))
        if self.sendToGateway != None:
            self.sendToGateway(gwMacAddr, payloadToGw)
        else:
            self.logger.error("No sender function. Please call setGatewaySenderFn().")

    def processRawRxPayload(self, gatewayMacAddr, jsonDict):
        '''
        Process the JSON payload received as part of the PUSH_DATA packet.
        This method should be supplied as a callback to the connection layer/module.

        gatewayMacAddr: MAC address of the source gateway
        jsonDict: resulting dictionary after JSON object has been parsed
        '''

        ### Process gateway metadata
        eouTimestamp = jsonDict["tmst"] # in usec
        ulFreqMHz = jsonDict["freq"]
        ulDatarate = jsonDict["datr"]
        ulCodingrate = jsonDict["codr"]
        ulRssi = jsonDict["rssi"]
        ulChannel = self.getUplinkChannelFromFreq(ulDatarate, ulFreqMHz)

        self.logger.info("Got packet with tmst:%d freq:%f datarate:%s codr:%s" \
                         " rssi:%d"%(eouTimestamp, ulFreqMHz, ulDatarate, 
                                     ulCodingrate, ulRssi))

        # decode padded Base64 RF packet
        phyPayload = base64.b64decode(jsonDict["data"])

        ### Process the PHY payload, whose structure is:
        ### | MHDR | MACPayload | MIC |
        mhdrByte = bytearray(phyPayload[0])[0]
        macPayload = phyPayload[1:-4]
        mic = phyPayload[-4:]
        
        # MHDR: | (7..5) MType | (4..2) RFU | (1..0) Major |
        mtype = (mhdrByte >> 5) & 0b111

        if mtype == MTYPE_JOIN_REQUEST:
            appEUI = struct.unpack("<Q", macPayload[0:8])[0] # little endian
            devEUI = struct.unpack("<Q", macPayload[8:16])[0] # little endian
            devNonce = struct.unpack("<H",macPayload[16:18])[0] # little endian

            dev = self.getDevFromEUI(appEUI, devEUI)
            if dev == None:
                # Either the message is corrupted or the device is not
                # registered on the server.
                self.logger.info("Cannot get device from EUI")
                return -1

            # Check message integrity (MIC)
            if mic != dev.crypto.computeJoinMic(phyPayload[:-4]):
                # Bad MIC
                self.logger.info("Bad packet Message Integrity Code")
                return -2

            # Handle join request. Should allocate a network address for the
            # device. Generate an AppNonce. Generate a join-accept message.
            # Also should update internal variables such as the mapping from
            # devAddr to device object 
            with dev.lock:
                self.handleJoinRequest(dev, devNonce)

        elif mtype == MTYPE_UNCONFIRMED_DATA_UP:
            # Process the MAC payload, whose structure is:
            # | FHDR | FPort | FRMPayload |
            # where FHDR is:
            # | DevAddr | FCtrl | FCnt | Fopts |
            return -1
            #TODO: make sure the network ID in devAddr matches our network ID
        else:
            # Invalid MAC message type. Bail.
            return -1

        # Remember that this gateway has access to the device
        dev.bindWithGateway(gatewayMacAddr, ulRssi)

        # Signal that we have a downlink opportunity to this device
        self.doDownlinkToDev(dev, eouTimestamp, ulChannel, ulDatarate,
                             ulCodingrate)

    def genDevAddr(self):
        ''' 
        Generates a random deviec address that is not yet in the network.

        devAddr == | 7-bit NetworkID | 25-bit NetworkAddress |
        '''
        while True:
            networkID_shifted = self.networkID << 25
            networkAddr = random.randint(0, (1<<25)-1)
            devAddr = networkID_shifted | networkAddr
            if devAddr not in self.addrToDevMap:
                break
        
        return devAddr

    def handleJoinRequest(self, dev, devNonce):
        if False and dev.joined:
            # [TODO]: check devNonce to prevent replay attacks
            # if devNonce is different than before, rejoin
            self.logger.info("Device already joined in network")
            return

        devAddr = self.genDevAddr()
        self.addrToDevMap[devAddr] = dev
        dev.devAddr = devAddr
        appNonce = random.randint(0, (1<<24)-1)

        self.logger.info("[handleJoinRequest] Allocated devAddr %x"%devAddr)

        # derive the network session key and app session key
        bufStr = str(bytearray([appNonce & 0xFF,
                                (appNonce >> 8) & 0xFF,
                                (appNonce >> 16) & 0xFF,
                                self.netID & 0xFF,
                                (self.netID >> 8) & 0xFF,
                                (self.netID >> 16) & 0xFF,
                                devNonce & 0xFF,
                                (devNonce >> 8) & 0xFF,
                                0,0,0,0,0,0,0]))
        nwkSKeyStr = dev.crypto.deriveSessionKey(str(bytearray([0x01])) + \
                                                 bufStr)
        appSKeyStr = dev.crypto.deriveSessionKey(str(bytearray([0x02])) + \
                                                 bufStr)
        dev.setSessionKeys(nwkSKeyStr, appSKeyStr)

        #import pdb; pdb.set_trace()
        # compose the join-accept message
        mhdr = str(bytearray([(MTYPE_JOIN_ACCEPT << 5) | \
                               MAJOR_VERSION_LORAWAN]))
        payload = str(bytearray([ appNonce & 0xFF,
                                  (appNonce >> 8) & 0xFF,
                                  (appNonce >> 16) & 0xFF,
                                  self.netID & 0xFF,
                                  (self.netID >> 8) & 0xFF,
                                  (self.netID >> 16) & 0xFF,
                                  devAddr & 0xFF,
                                  (devAddr >> 8) & 0xFF,
                                  (devAddr >> 16) & 0xFF,
                                  (devAddr >> 24) & 0xFF,
                                  0, # DLSettings
                                  0, # RxDelay
                                ]))
        mic = dev.crypto.computeJoinMic(mhdr + payload)

        # encrypt the payload (not including MAC header and MIC)
        bodyEncrypted = dev.crypto.encryptJoinAccept(payload + mic)

        # Queue the downlink. Will be picked up by doDownlinkToDev()
        dev.joined = True
        dlMsg = DownlinkMessage(mhdr + bodyEncrypted, JOIN_ACCEPT_WINDOW_1)
        dev.putDownlinkMsg(dlMsg)

        self.logger.info("[handleJoinRequest] Join accept msg downlink queued")

class DownlinkMessage:
    def __init__(self, payloadByteStr, rxWindow):
        self.payloadSize = len(payloadByteStr)
        self.rxWindow = rxWindow
        self.payloadBase64 = base64.b64encode(payloadByteStr)