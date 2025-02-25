# Copyright (c) 2018  Kontron Europe GmbH
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA

import os
import socket
import struct
import hashlib
import hmac
import random
import threading
from array import array
from queue import Queue
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .. import Target
from ..session import Session
from ..msgs import (create_message, create_request_by_name,
                    encode_message, decode_message, constants)
from ..msgs import Message, Bitfield, String, UnsignedInt, MessageStatusCode, VariableByteArray, CompletionCode, RemainingBytes
from ..messaging import ChannelAuthenticationCapabilities
from ..errors import DecodingError, NotSupportedError
from ..logger import log
from ..interfaces.ipmb import (IpmbHeaderReq, encode_ipmb_msg,
                               encode_bridged_message, decode_bridged_message,
                               rx_filter)
from ..utils import check_completion_code, py3_array_tobytes


CLASS_NORMAL_MSG = 0x00
CLASS_ACK_MSG = 0x80

RMCP_CLASS_ASF = 0x06
RMCP_CLASS_IPMI = 0x07
RMCP_CLASS_OEM = 0x08


def call_repeatedly(interval, func, *args):
    stopped = threading.Event()

    def loop():
        # the first call is in `interval` secs
        while not stopped.wait(interval):
            try:
                func(*args)
            except socket.timeout:
                pass

    t = threading.Thread(target=loop)
    t.daemon = True
    t.start()

    return stopped.set


class RmcpMsg(object):
    RMCP_HEADER_FORMAT = '!BxBB'
    ASF_RMCP_V_1_0 = 6
    version = None
    seq_number = None
    class_of_msg = None

    def __init__(self, class_of_msg=None):
        if class_of_msg is not None:
            self.class_of_msg = class_of_msg

    def pack(self, sdu, seq_number):
        pdu = struct.pack(self.RMCP_HEADER_FORMAT, self.ASF_RMCP_V_1_0,
                          seq_number, self.class_of_msg)
        if sdu is not None:
            pdu += sdu
        return pdu

    def unpack(self, pdu):
        header_len = struct.calcsize(self.RMCP_HEADER_FORMAT)
        header = pdu[:header_len]
        (self.version, self.seq_number, self.class_of_msg) = \
            struct.unpack(self.RMCP_HEADER_FORMAT, header)
        sdu = pdu[header_len:]

        if self.version != self.ASF_RMCP_V_1_0:
            raise DecodingError('invalid RMCP version field')

        return sdu


class AsfMsg(object):
    ASF_HEADER_FORMAT = '!IBBxB'

    ASF_TYPE_PRESENCE_PONG = 0x40
    ASF_TYPE_PRESENCE_PING = 0x80

    asf_type = 0

    def __init__(self):
        self.iana_enterprise_number = 4542
        self.tag = 0
        self.data = None
        self.sdu = None

    def pack(self):
        if self.data:
            data_len = len(self.data)
        else:
            data_len = 0

        pdu = struct.pack(self.ASF_HEADER_FORMAT,
                          self.iana_enterprise_number,
                          self.asf_type,
                          self.tag,
                          data_len)
        if self.data:
            pdu += self.data

        return pdu

    def unpack(self, sdu):
        self.sdu = sdu
        header_len = struct.calcsize(self.ASF_HEADER_FORMAT)

        header = sdu[:header_len]
        (self.iana_enterprise_number, self.asf_type, self.tag, data_len) = \
            struct.unpack(self.ASF_HEADER_FORMAT, header)

        if len(sdu) < header_len + data_len:
            raise DecodingError('short SDU')
        elif len(sdu) > header_len + data_len:
            raise DecodingError('SDU has extra bytes')

        if data_len != 0:
            self.data = sdu[header_len:header_len + data_len]
        else:
            self.data = None

        if hasattr(self, 'check_header'):
            self.check_header()

    def __str__(self):
        if self.data:
            return ' '.join('%02x' % b for b in array('B', self.data))
        if self.sdu:
            return ' '.join('%02x' % b for b in array('B', self.sdu))
        return ''

    @staticmethod
    def from_data(sdu):
        asf = AsfMsg()
        asf.unpack(sdu)

        try:
            cls = {
                AsfMsg().ASF_TYPE_PRESENCE_PING: AsfPing,
                AsfMsg().ASF_TYPE_PRESENCE_PONG: AsfPong,
            }[asf.asf_type]
        except KeyError:
            raise DecodingError('Unsupported ASF type(0x%02x)' % asf.asf_type)

        instance = cls()
        instance.unpack(sdu)
        return instance


class AsfPing(AsfMsg):
    def __init__(self):
        AsfMsg.__init__(self)
        self.asf_type = self.ASF_TYPE_PRESENCE_PING

    def check_header(self):
        if self.asf_type != self.ASF_TYPE_PRESENCE_PING:
            raise DecodingError('type does not match')
        if self.data:
            raise DecodingError('Data length is not zero')

    def __str__(self):
        return 'ping: ' + super(AsfMsg, self).__str__()


class AsfPong(AsfMsg):
    DATA_FORMAT = '!IIBB6x'

    def __init__(self):
        self.asf_type = self.ASF_TYPE_PRESENCE_PONG
        self.oem_iana_enterprise_number = 4542
        self.oem_defined = 0
        self.supported_entities = 0
        self.supported_interactions = 0

    def pack(self):
        pdu = struct.pack(self.DATA_FORMAT,
                          self.oem_iana_enterprise_number,
                          self.oem_defined,
                          self.supported_entities,
                          self.supported_interactions)
        return pdu

    def unpack(self, sdu):
        AsfMsg.unpack(self, sdu)
        # header_len = struct.calcsize(self.ASF_HEADER_FORMAT)
        (self.oem_iana_enterprise_number, self.oem_defined,
            self.supported_entities, self.supported_interactions) =\
            struct.unpack(self.DATA_FORMAT, self.data)

        self.check_data()

    def check_data(self):
        if self.oem_iana_enterprise_number == 4542 and self.oem_defined != 0:
            raise DecodingError('SDU malformed')
        if self.supported_interactions != 0:
            raise DecodingError('SDU malformed')

    def check_header(self):
        if self.asf_type != self.ASF_TYPE_PRESENCE_PONG:
            raise DecodingError('type does not match')
        if len(self.data) != struct.calcsize(self.DATA_FORMAT):
            raise DecodingError('Data length mismatch')


class OpenSessionReq(Message):
    __fields__ = (
        UnsignedInt('message_tag', 1, 0),
        Bitfield('maximum_privilege', 1,
                 Bitfield.Bit('privilege_level', 4, 0),
                 Bitfield.ReservedBit(4, 0),
                 ),
        Bitfield('reserved', 2,
                 Bitfield.ReservedBit(16, 0)
                 ),
        String('console_session_id', 4, b"\xA1\xA2\xA3\xA4"), # ipmitool uses 0xA4A3A2A0
        # Authentication payload
        Bitfield('authentication', 8,
                 Bitfield.Bit('type', 8, 0),
                 Bitfield.ReservedBit(16, 0),
                 Bitfield.Bit('length', 8, 0x08),
                 Bitfield.Bit('algorithm', 6, constants.AUTH_ALGO_RAKP_HMAC_SHA1),
                 Bitfield.ReservedBit(2, 0),
                 Bitfield.ReservedBit(24, 0)
                 ),
        # Integrity payload
        Bitfield('integrity', 8,
                 Bitfield.Bit('type', 8, 0x01),
                 Bitfield.ReservedBit(16, 0),
                 Bitfield.Bit('length', 8, 0x08),
                 Bitfield.Bit('algorithm', 6, constants.INTEGRITY_ALGO_HMAC_SHA1_96),
                 Bitfield.ReservedBit(2, 0),
                 Bitfield.ReservedBit(24, 0)
                 ),
        # Confidentiality payload
        Bitfield('confidentiality', 8,
                 Bitfield.Bit('type', 8, 0x02),
                 Bitfield.ReservedBit(16, 0),
                 Bitfield.Bit('length', 8, 0x08),
                 Bitfield.Bit('algorithm', 6, constants.CONFIDENTIALITY_ALGO_AES_CBC_128 ),
                 Bitfield.ReservedBit(2, 0),
                 Bitfield.ReservedBit(24, 0)
                 ),
    )
    def __init__(self, authentication_algo, integrity_algo, confidentiality_algo, *args, **kwargs):
        Message.__init__(self, *args, **kwargs)
        self.authentication.algorithm = authentication_algo
        self.integrity.algorithm = integrity_algo
        self.confidentiality.algorithm = confidentiality_algo


class OpenSessionRsp(Message):
    __fields__ = (
        UnsignedInt('message_tag', 1, 0),
        MessageStatusCode(),
        Bitfield('maximum_privilege', 1,
                 Bitfield.Bit('privilege_level', 4, 0),
                 Bitfield.ReservedBit(4, 0),
                 ),
        Bitfield('reserved', 1,
                 Bitfield.ReservedBit(8, 0)
                 ),
        String('console_session_id', 4, "\x00" * 4),
        String('managed_system_session_id', 4, "\x00" * 4),
        # Authentication payload
        Bitfield('authentication', 8,
                 Bitfield.Bit('type', 8, 0),
                 Bitfield.ReservedBit(16, 0),
                 Bitfield.Bit('length', 8, 0x08),
                 Bitfield.Bit('algorithm', 6, 0),
                 Bitfield.ReservedBit(2, 0),
                 Bitfield.ReservedBit(24, 0)
                 ),
        # Integrity payload
        Bitfield('integrity', 8,
                 Bitfield.Bit('type', 8, 0x01),
                 Bitfield.ReservedBit(16, 0),
                 Bitfield.Bit('length', 8, 0x08),
                 Bitfield.Bit('algorithm', 6, 0),
                 Bitfield.ReservedBit(2, 0),
                 Bitfield.ReservedBit(24, 0)
                 ),
        # Confidentiality payload
        Bitfield('confidentiality', 8,
                 Bitfield.Bit('type', 8, 0x02),
                 Bitfield.ReservedBit(16, 0),
                 Bitfield.Bit('length', 8, 0x08),
                 Bitfield.Bit('algorithm', 6, 0),
                 Bitfield.ReservedBit(2, 0),
                 Bitfield.ReservedBit(24, 0)
                 ),
    )

class RAKP1Message(Message):
    __fields__ = (
        UnsignedInt('message_tag', 1, 0),
        Bitfield('reserved1', 3,
                 Bitfield.ReservedBit(24, 0)
                 ),
        String('managed_system_session_id', 4, "\x00" * 4),
        String('console_random_number', 16, os.urandom(16)),
        Bitfield('role', 1,
                 Bitfield.Bit('privilege_level', 4, 0x4), # ADMINISTRATOR level
                 Bitfield.Bit('lookup', 1, 1),  # Name-only lookup
                 Bitfield.ReservedBit(3, 0),
                 ),
        Bitfield('reserved2', 2,
                 Bitfield.ReservedBit(16, 0),
                 ),
        UnsignedInt('user_name_length', 1, 0),
        String('user_name', 16, '\x00' * 16),
    )


class RAKP2Message(Message):
    def _length_ke_auth_code(obj):
        if obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_NONE:
            return 0
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_SHA1:
            return 20
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_MD5:
            return 16
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_SHA256:
            return 32
    __fields__ = (
        UnsignedInt('message_tag', 1, 0),
        MessageStatusCode(),
        Bitfield('reserved1', 2,
                 Bitfield.ReservedBit(16, 0)
                 ),
        String('console_session_id', 4, "\x00" * 4),
        String('managed_system_random_number', 16, "\x00"* 16),
        String('managed_system_guid', 16, "\x00" * 16),
        VariableByteArray('ke_auth_code', _length_ke_auth_code),
    )
    def __init__(self, authentication_algorithm, *args, **kwargs):
        Message.__init__(self, *args, **kwargs)
        self.authentication_algorithm = authentication_algorithm


class RAKP3Message(Message):
    def _length_ke_auth_code(obj):
        if obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_NONE:
            return 0
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_SHA1:
            return 20
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_MD5:
            return 16
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_SHA256:
            return 32
    __fields__ = (
        UnsignedInt('message_tag', 1, 0),
        MessageStatusCode(),
        Bitfield('reserved1', 2,
                 Bitfield.ReservedBit(16, 0)
                 ),
        String('managed_system_session_id', 4, '\x00' * 4),
        VariableByteArray('ke_auth_code', _length_ke_auth_code)

    )
    def __init__(self, authentication_algorithm, *args, **kwargs):
        Message.__init__(self, *args, **kwargs)
        self.authentication_algorithm = authentication_algorithm

class RAKP4Message(Message):
    def _length_integrity_check_value(obj):
        if obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_NONE:
            return 0
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_SHA1:
            return 12
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_MD5:
            return 16
        elif obj.authentication_algorithm is constants.AUTH_ALGO_RAKP_HMAC_SHA256:
            return 16
    __fields__ = (
        UnsignedInt('message_tag', 1, 0),
        MessageStatusCode(),
        Bitfield('reserved1', 2,
                 Bitfield.ReservedBit(16, 0)
                 ),
        String('console_session_id', 4, "\x00" * 4),
        VariableByteArray('integrity_check_value', _length_integrity_check_value)
    )
    def __init__(self, authentication_algorithm, *args, **kwargs):
        Message.__init__(self, *args, **kwargs)
        self.authentication_algorithm = authentication_algorithm

class GetPayloadActivationStatusReq(Message):
    __fields__ = (
        UnsignedInt('payload_type', 1, 0),
    )

class GetPayloadActivationStatusRsp(Message):
    __fields__ = (
        CompletionCode(),
        Bitfield('instance_capacity', 1,
                 Bitfield.ReservedBit(4, 0),
                 Bitfield.Bit('capacity', 4, 0)
                 ),
        Bitfield('instance_bitmap', 2,
                 Bitfield.Bit('bitmap1_8', 8, 0),
                 Bitfield.Bit('bitmap9_16', 8, 0),
                 ),
    )

class SOLPayloadRCToBMC(Message):
    __fields__ = (
        Bitfield("packet_seq", 1,
                 Bitfield.Bit('number', 4, 0),
                 Bitfield.ReservedBit(4, 0),
                 ),
        Bitfield('packet_ack_seq', 1,
                 Bitfield.Bit('number', 4, 0),
                 Bitfield.ReservedBit(4, 0),
                 ),
        UnsignedInt('character_count', 1, 0),
        Bitfield('operation_status', 1,
                 Bitfield.Bit("flush_outbound", 1, 0),
                 Bitfield.Bit("flush_inbound", 1, 0),
                 Bitfield.Bit("dcd_dsr", 1, 0),
                 Bitfield.Bit("cts", 1, 0),
                 Bitfield.Bit("break", 1, 0),
                 Bitfield.Bit("ring_wor", 1, 0),
                 Bitfield.Bit("ack_nack", 1, 0),
                 Bitfield.ReservedBit(1, 0),
                 ),
        RemainingBytes('data'),
    )

class SOLPayloadBMCToRC(Message):
    __fields__ = (
        Bitfield("packet_seq", 1,
                 Bitfield.Bit('number', 4, 0),
                 Bitfield.ReservedBit(4, 0),
                 ),
        Bitfield('packet_ack_seq', 1,
                 Bitfield.Bit('number', 4, 0),
                 Bitfield.ReservedBit(4, 0),
                 ),
        UnsignedInt('character_count', 1, 0),
        Bitfield('operation_status', 1,
                 Bitfield.Bit("flush_outbound", 1, 0),
                 Bitfield.Bit("flush_inbound", 1, 0),
                 Bitfield.Bit("dcd_dsr", 1, 0),
                 Bitfield.Bit("cts", 1, 0),
                 Bitfield.Bit("break", 1, 0),
                 Bitfield.Bit("ring_wor", 1, 0),
                 Bitfield.Bit("ack_nack", 1, 0),
                 Bitfield.ReservedBit(1, 0),
                 ),
        RemainingBytes('data'),
    )

class Ipmi20Msg(object):
    """Message for RMCP+"""
    HEADER_FORMAT = "!BBIIBB"
    def __init__(self, session=None):
        self.session = session

    def _pack_session_id(self):
        if self.session is not None:
            session_id = int.from_bytes(self.session.sid, "big")
        else:
            session_id = 0
        return session_id

    def _pack_sequence_number(self):
        if self.session is not None:
            seq = self.session.sequence_number
        else:
            seq = 0

        return struct.unpack("<I", struct.pack(">I", seq))[0]

    def _pack_payload_type(self, payload_type):
        if self.session is None:
            final_payload_type = 0
        else:
            if self.session.is_authenticated:
                final_payload_type = 1 << 7
            if self.session.is_encrypted:
                final_payload_type |= 1 << 6
        final_payload_type |= payload_type
        return final_payload_type

    def pack(self, sdu, payload_type):
        if sdu is not None:
            data_len = len(sdu)
        else:
            data_len = 0

        if self.session is not None:
            auth_type = self.session.auth_type
            if self.session.activated:
                self.session.increment_sequence_number()
        else:
            auth_type = Session.AUTH_TYPE_RMCP_PLUS

        pdu = struct.pack('!BBII',  # TODO: Verify if the packing is network or little endian
                          auth_type,
                          self._pack_payload_type(payload_type),
                          self._pack_session_id(),
                          self._pack_sequence_number(),
                          )

        if sdu is None:
            pdu += py3_array_tobytes(array('B', list(data_len.to_bytes(2, 'little'))))
            return pdu

        if self.session is None or self.session.is_encrypted is False:
            pdu += py3_array_tobytes(array('B', list(data_len.to_bytes(2, 'little'))))
            pdu += sdu
            return pdu

        # Now the encryption starts
        # 1. Pad the input
        # TODO: move this to constant
        block_size = 16
        mod = (len(sdu) + 1) % block_size
        if mod:
            pad_length = block_size - mod
            for i in range(pad_length):
                sdu += (i+1).to_bytes(1, "big")
        sdu += pad_length.to_bytes(1, "big")
        # Now sdu contains the padded payload

        # 2. Generate IV
        initialization_vector = os.urandom(block_size)

        # 3. Get K, K2
        (k_1, k_2) = self.session.additional_encryption_keys
        cipher_key = k_2[:16]
        cipher = Cipher(algorithms.AES(cipher_key), modes.CBC(initialization_vector))
        encryptor = cipher.encryptor()

        # 4. Encrypt
        ct = encryptor.update(sdu) + encryptor.finalize()


        data_len = len(initialization_vector + ct)
        pdu += py3_array_tobytes(array('B', list(data_len.to_bytes(2, 'little'))))
        pdu += initialization_vector + ct

        # Now add the Session trailer
        # Auth code is either 12 or 16 bytes. Adding pad length (1 byte) and next header (1 byte)
        # the total is either 14 or 18. So, to make it to a multiple of 4, we need to add 2 byte
        # padding
        session_trailer = bytes.fromhex("ffff") # padding of 2 bytes
        padding_length = 2
        session_trailer += padding_length.to_bytes(1, "big")
        next_header = 0x07 # Next header should be 0x07 as per spec
        session_trailer += next_header.to_bytes(1, "big")
        pdu += session_trailer

        # Calculate the auth code
        auth_code = hmac.new(k_1, pdu, hashlib.sha1).digest()
        auth_code = auth_code[:12]  # for HMAC-SHA1-96

        pdu += auth_code
        return pdu

    def unpack(self, pdu):
        header_len = struct.calcsize(self.HEADER_FORMAT)
        header = pdu[:header_len]
        (auth_type, payload_type, self.session_id, sequence_number, data_len_1, data_len_2) = \
            struct.unpack(self.HEADER_FORMAT, header)

        # The returned data is little endian. Conbine the two bytes for final length value
        data_len = (data_len_2 << 8) + data_len_1
        if self.session is not None and self.session.activated and self.session.is_encrypted:
            trailer_len = 16
        else:
            trailer_len = 0
        if len(pdu) < header_len + data_len + trailer_len:
            raise DecodingError('short SDU ({:d},{:d},{:d},{:d})'.format(
                len(pdu), header_len, data_len, trailer_len))
        elif len(pdu) > header_len + data_len + trailer_len:
            raise DecodingError('SDU has extra bytes ({:d},{:d},{:d},{:d})'.format(
                len(pdu), header_len, data_len, trailer_len))

        if data_len <= 0:
            return None

        if self.session is None or self.session.is_encrypted is False:
            return pdu[header_len:header_len + data_len]
        # Session is encrypted. Decrypt the data
        (k_1, k_2) = self.session.additional_encryption_keys
        cipher_key = k_2[:16]
        iv = pdu[header_len:header_len + 16]
        ct = pdu[header_len+16:header_len+data_len]
        cipher = Cipher(algorithms.AES(cipher_key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        sdu = decryptor.update(ct) + decryptor.finalize()
        # Remove padding
        padding_length = sdu[-1] #int.from_bytes(sdu[-1], "little")
        sdu = sdu[:-(padding_length+1)]
        # TODO: Verify auth code

        return sdu

class IpmiMsg(object):
    HEADER_FORMAT_NO_AUTH = '!BIIB'
    HEADER_FORMAT_AUTH = '!BII16BB'

    def __init__(self, session=None):
        self.session = session

    def _pack_session_id(self):
        if self.session is not None:
            session_id = self.session.sid
        else:
            session_id = 0
        return struct.unpack("<I", struct.pack(">I", session_id))[0]

    def _pack_sequence_number(self):
        if self.session is not None:
            seq = self.session.sequence_number
        else:
            seq = 0

        return struct.unpack("<I", struct.pack(">I", seq))[0]

    def _padd_password(self):
        """Padd the password.

        The password/key is 0 padded to 16-bytes for all specified
        authentication types.
        """
        password = self.session._auth_password
        if isinstance(password, str):
            password = str.encode(password)
        return password.ljust(16, b'\x00')

    def _pack_auth_code_straight(self):
        """Return the auth code as bytestring."""
        return self._padd_password()

    def _pack_auth_code_md5(self, sdu):
        auth_code = struct.pack('>16s I %ds I 16s' % len(sdu),
                                self._pack_auth_code_straight(),
                                self._pack_session_id(),
                                sdu,
                                self._pack_sequence_number(),
                                self._pack_auth_code_straight())
        return hashlib.md5(auth_code).digest()

    def pack(self, sdu):
        if sdu is not None:
            data_len = len(sdu)
        else:
            data_len = 0

        if self.session is not None:
            auth_type = self.session.auth_type
            if self.session.activated:
                self.session.increment_sequence_number()
        else:
            auth_type = Session.AUTH_TYPE_NONE

        pdu = struct.pack('!BII',
                          auth_type,
                          self._pack_sequence_number(),
                          self._pack_session_id())

        if auth_type == Session.AUTH_TYPE_NONE:
            pass
        elif auth_type == Session.AUTH_TYPE_PASSWORD:
            pdu += self._pack_auth_code_straight()
        elif auth_type == Session.AUTH_TYPE_MD5:
            pdu += self._pack_auth_code_md5(sdu)
        else:
            raise NotSupportedError('authentication type %s' % auth_type)

        pdu += py3_array_tobytes(array('B', [data_len]))

        if sdu is not None:
            pdu += sdu

        return pdu

    def unpack(self, pdu):
        auth_type = array('B', pdu)[0]

        if auth_type != 0:
            header_len = struct.calcsize(self.HEADER_FORMAT_AUTH)
            header = pdu[:header_len]
            # TBD .. find a way to do this better
            self.auth_type = array('B', pdu)[0]
            (self.sequence_number,) = struct.unpack('!I', pdu[1:5])
            (self.session_id,) = struct.unpack('!I', pdu[5:9])
            self.auth_code = [a for a in struct.unpack('!16B', pdu[9:25])]
            data_len = array('B', pdu)[25]
        else:
            header_len = struct.calcsize(self.HEADER_FORMAT_NO_AUTH)
            header = pdu[:header_len]
            (self.auth_type, self.sequence_number, self.session_id,
                data_len) = struct.unpack(self.HEADER_FORMAT_NO_AUTH, header)

        if len(pdu) < header_len + data_len:
            raise DecodingError('short SDU')
        elif len(pdu) > header_len + data_len:
            raise DecodingError('SDU has extra bytes ({:d},{:d},{:d} )'.format(
                len(pdu), header_len, data_len))

        if hasattr(self, 'check_header'):
            self.check_header()

        if data_len != 0:
            sdu = pdu[header_len:header_len + data_len]
        else:
            sdu = None

        return sdu

    def check_data(self):
        pass

    def check_header(self):
        pass


class Rmcp(object):
    NAME = 'rmcp'

    _session = None

    def __init__(self, slave_address=0x81, host_target_address=0x20,
                 keep_alive_interval=1, timeout=2.0):
        self.host = None
        self.port = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq_number = 0xff
        self.slave_address = slave_address
        self.host_target = Target(host_target_address)
        self.set_timeout(timeout)
        self.next_sequence_number = 0
        self.keep_alive_interval = keep_alive_interval
        self._stop_keep_alive = None
        self._q = Queue()
        self.transaction_lock = threading.Lock()

    def _send_rmcp_msg(self, sdu, class_of_msg):
        rmcp = RmcpMsg(class_of_msg)
        pdu = rmcp.pack(sdu, self.seq_number)
        self._sock.sendto(pdu, (self.host, self.port))
        if self.seq_number != 255:
            self.seq_number = (self.seq_number + 1) % 254

    def _receive_rmcp_msg(self):
        (pdu, _) = self._sock.recvfrom(4096)
        rmcp = RmcpMsg()
        sdu = rmcp.unpack(pdu)
        return (rmcp.seq_number, rmcp.class_of_msg, sdu)

    def set_timeout(self, timeout):
        self._sock.settimeout(timeout)

    def _send_ipmi_msg(self, data):
        log().debug('IPMI TX: {:s}'.format(
            ' '.join('%02x' % b for b in array('B', data))))
        ipmi = IpmiMsg(self._session)
        tx_data = ipmi.pack(data)
        self._send_rmcp_msg(tx_data, RMCP_CLASS_IPMI)

    def _send_ipmi2_msg(self, data, payload_type):
        log().debug('IPMI2.0 TX: {:s}'.format(
            ' '.join('%02x' % b for b in array('B', data))))
        ipmi = Ipmi20Msg(self._session)
        tx_data = ipmi.pack(data, payload_type)
        self._send_rmcp_msg(tx_data, RMCP_CLASS_IPMI)

    def _receive_ipmi2_msg(self):
        (_, class_of_msg, pdu) = self._receive_rmcp_msg()
        if class_of_msg != RMCP_CLASS_IPMI:
            raise DecodingError('invalid class field in ASF message')
        msg = Ipmi20Msg(self._session)
        data = msg.unpack(pdu)
        log().debug('IPMI2.0 RX: {:s}'.format(
            ' '.join('%02x' % b for b in array('B', data))))
        return data

    def _receive_ipmi_msg(self):
        (_, class_of_msg, pdu) = self._receive_rmcp_msg()
        if class_of_msg != RMCP_CLASS_IPMI:
            raise DecodingError('invalid class field in ASF message')
        msg = IpmiMsg()
        data = msg.unpack(pdu)
        log().debug('IPMI RX: {:s}'.format(
            ' '.join('%02x' % b for b in array('B', data))))
        return data

    def _send_asf_msg(self, msg):
        log().debug('ASF TX: msg')
        self._send_rmcp_msg(msg.pack(), RMCP_CLASS_ASF)

    def _receive_asf_msg(self, cls):
        (_, class_of_msg, data) = self._receive_rmcp_msg()
        log().debug('ASF RX: msg')
        if class_of_msg != RMCP_CLASS_ASF:
            raise DecodingError('invalid class field in ASF message')
        msg = cls()
        msg.unpack(data)
        return msg

    def ping(self):
        ping = AsfPing()
        self._send_asf_msg(ping)
        self._receive_asf_msg(AsfPong)

    def _get_channel_auth_cap(self):
        CHANNEL_NUMBER_FOR_THIS = 0xe
        # get channel auth cap
        req = create_request_by_name('GetChannelAuthenticationCapabilities')
        req.target = self.host_target
        req.channel.number = CHANNEL_NUMBER_FOR_THIS
        req.privilege_level.requested = Session.PRIV_LEVEL_ADMINISTRATOR
        rsp = self.send_and_receive(req)
        check_completion_code(rsp.completion_code)
        caps = ChannelAuthenticationCapabilities(rsp)
        return caps

    def _get_session_challenge(self, session):
        # get session challenge
        req = create_request_by_name('GetSessionChallenge')
        req.target = self.host_target
        req.authentication.type = session.auth_type
        if session._auth_username:
            req.user_name = session._auth_username.ljust(16, '\x00')
        rsp = self.send_and_receive(req)
        check_completion_code(rsp.completion_code)
        return rsp

    def _open_session(self, session, auth_algo, integrity_algo, confidentiality_algo):
        req = OpenSessionReq(auth_algo, integrity_algo, confidentiality_algo)
        req.maximum_privilege.privilege_level = Session.PRIV_LEVEL_ADMINISTRATOR
        rx_data = self._send_and_receive_rmcp2(encode_message(req),
                                           constants.PAYLOAD_TYPE_OPEN_SESSION_REQUEST)
        rsp = OpenSessionRsp()
        decode_message(rsp, rx_data)

        # TODO: Use proper exceptions
        if rsp.message_status_code != constants.MSC_OK:
            raise Exception("Open Session response failed")
        # TODO: Check if authentication algorithm matches with what we asked for
        rakp1 = RAKP1Message()
        rakp1.message_tag = req.message_tag
        rakp1.managed_system_session_id = rsp.managed_system_session_id
        rakp1.user_name = session._auth_username
        rakp1.user_name_length = len(session._auth_username)
        rx_data = self._send_and_receive_rmcp2(encode_message(rakp1),
                                               constants.PAYLOAD_TYPE_RAKP_MESSAGE_1)
        rakp2 = RAKP2Message(req.authentication.algorithm)
        decode_message(rakp2, rx_data)
        # TODO: Use proper exceptions
        if rakp2.message_status_code != constants.MSC_OK:
            raise Exception("Open RAKP2 response failed")

        if rakp2.console_session_id != req.console_session_id:
            raise Exception(f"Invalid Console session id in RAKP2 packet: {rakp2.console_session_id}")
        auth_code = b"".join([c.to_bytes(1, 'big') for c in rakp2.ke_auth_code])
        sid_m = rakp2.console_session_id
        sid_c = rsp.managed_system_session_id
        r_m = rakp1.console_random_number
        r_c = rakp2.managed_system_random_number
        guid_c = rakp2.managed_system_guid
        role_m = rakp1.role._value.to_bytes(1, 'big')
        ulength_m = rakp1.user_name_length.to_bytes(1, 'big')
        uname_m = rakp1.user_name.encode()
        unencrypted_string = sid_m + sid_c + r_m + r_c + guid_c + role_m + ulength_m + uname_m
        key = session._auth_password.encode()
        expected_auth_code = hmac.new(key, unencrypted_string, hashlib.sha1).digest()
        if expected_auth_code != auth_code:
            raise Exception("Auth code mismatch")
        rakp3 = RAKP3Message(req.authentication.algorithm)
        rakp3.managed_system_session_id = sid_c
        rakp3.ke_auth_code = hmac.new(key, r_c + sid_m + role_m + ulength_m + uname_m, hashlib.sha1).digest()
        rx_data = self._send_and_receive_rmcp2(encode_message(rakp3),
                                               constants.PAYLOAD_TYPE_RAKP_MESSAGE_3)
        rakp4 = RAKP4Message(req.authentication.algorithm)
        decode_message(rakp4, rx_data)
        sik = hmac.new(key, r_m + r_c + role_m + ulength_m + uname_m, hashlib.sha1).digest()
        expected_integrity_check_value = hmac.new(sik, r_m + sid_c + guid_c, hashlib.sha1).digest()
        expected_integrity_check_value = expected_integrity_check_value[:12]
        integrity_check_value = b"".join([c.to_bytes(1, 'big') for c in rakp4.integrity_check_value])
        if expected_integrity_check_value != integrity_check_value:
            raise Exception(f"Integrity check value mismatch: {expected_integrity_check_value} vs {integrity_check_value}")
        return (sik, sid_c)

    def _activate_session(self, session, challenge):
        # activate session
        req = create_request_by_name('ActivateSession')
        req.target = self.host_target
        req.authentication.type = session.auth_type
        req.privilege_level.maximum_requested =\
            Session.PRIV_LEVEL_ADMINISTRATOR
        req.challenge_string = challenge
        req.session_id = self._session.sid
        req.initial_outbound_sequence_number = random.randrange(1, 0xffffffff)
        rsp = self.send_and_receive(req)
        check_completion_code(rsp.completion_code)
        return rsp

    def _set_session_privilege_level(self, level):
        req = create_request_by_name('SetSessionPrivilegeLevel')
        req.target = self.host_target
        req.privilege_level.requested = level
        rsp = self.send_and_receive(req)
        check_completion_code(rsp.completion_code)
        return rsp

    def _get_device_id(self):
        req = create_request_by_name('GetDeviceId')
        req.target = self.host_target
        rsp = self.send_and_receive(req)
        check_completion_code(rsp.completion_code)

    def start_sol(self):
        if self._session is None or self._session.activated is not True:
            raise Exception("Session is not activated")
        req = create_request_by_name("SetSessionPrivilegeLevel")
        req.privilege_level.requested = Session.PRIV_LEVEL_ADMINISTRATOR
        rsp = self.send_and_receive_rmcp2(req, constants.PAYLOAD_TYPE_IPMI)
        if rsp.completion_code != constants.CC_OK:
            raise Exception("Set session privilege level failed")
        req = create_request_by_name("ActivateSOLPayload")
        rsp = self.send_and_receive_rmcp2(req, constants.PAYLOAD_TYPE_IPMI)
        if rsp.completion_code != constants.CC_OK:
            raise Exception("Activate SOL payload failed")
        # We need to set timeout to None before we start SOL so that we don't get a timeout
        # self.set_timeout(0.0)
        # Start SOL now
        with self.transaction_lock:
            # Send \r to get a response
            seq_number = 1
            packet_ack_seq = 0
            accepted_character_count = 0
            rc_to_bmc = SOLPayloadRCToBMC()
            rc_to_bmc.data = b'\r'
            while True:
                if rc_to_bmc is not None:
                    rc_to_bmc.packet_seq.number = seq_number
                    rc_to_bmc.packet_ack_seq.number = packet_ack_seq
                    rc_to_bmc.character_count = accepted_character_count
                    tx_data = encode_message(rc_to_bmc)
                    self._send_ipmi2_msg(tx_data, constants.PAYLOAD_TYPE_SOL)
                try:
                    rx_data = self._receive_ipmi2_msg()
                except socket.timeout:
                    continue

                bmc_to_rc = SOLPayloadBMCToRC()
                decode_message(bmc_to_rc, rx_data)
                # TODO: Check bmc_to_rc.packet_ack_seq with seq_number
                packet_ack_seq = bmc_to_rc.packet_seq.number
                accepted_character_count = len(bmc_to_rc.data)
                if accepted_character_count == 0:
                    # This is a ACK only packet
                    # No need to send an acknowledgement
                    rc_to_bmc = None
                    continue
                data = bmc_to_rc.data.tobytes()
                try:
                    data = data.decode()
                except UnicodeDecodeError:
                    # print(data)
                    pass
                print(data, end="")
                seq_number = 0  # ACK-only packet needs no sequence number
                rc_to_bmc = SOLPayloadRCToBMC()
                rc_to_bmc.data = b""


        return rsp

    def establish_session(self, session):
        self._session = None
        self.host = session._rmcp_host
        self.port = session._rmcp_port

        # 0 - Ping
        self.ping()

        # 1 - Get Channel Authentication Capabilities
        log().debug('Get Channel Authentication Capabilities')
        caps = self._get_channel_auth_cap()
        log().debug('%s' % caps)

        session.auth_type = caps.get_max_auth_type()
        if caps.ipmi_2_0 is True:
            # Send RMCP+ Open Session Request
            log().debug("Open Session Request")
            (sik, session_id ) = self._open_session(session,
                                                    constants.AUTH_ALGO_RAKP_HMAC_SHA1,
                                                    constants.INTEGRITY_ALGO_HMAC_SHA1_96,
                                                    constants.CONFIDENTIALITY_ALGO_AES_CBC_128)
            session.sid = session_id
            session.sequence_number = 0x02
            session.activated = True
            session.is_encrypted = True
            session.is_authenticated = True
            session.auth_type = caps.get_max_auth_type()
            session.confidentiality_algorithm = constants.CONFIDENTIALITY_ALGO_AES_CBC_128
            session.generate_additional_encryption_keys(sik)
            self._session = session
        elif caps.ipmi_1_5 is True:
            # 2 - Get Session Challenge
            log().debug('Get Session Challenge')
            rsp = self._get_session_challenge(session)
            session_challenge = rsp.challenge_string
            session.sid = rsp.temporary_session_id
            self._session = session
            # 3 - Activate Session
            log().debug('Activate Session')
            rsp = self._activate_session(session, session_challenge)
            self._session.sid = rsp.session_id
            self._session.sequence_number = rsp.initial_inbound_sequence_number
            self._session.activated = True

            log().debug('Set Session Privilege Level')
            # 4 - Set Session Privilege Level
            self._set_session_privilege_level(Session.PRIV_LEVEL_ADMINISTRATOR)

            log().debug('Session opened')

            if self.keep_alive_interval:
                self._stop_keep_alive = call_repeatedly(
                        self.keep_alive_interval, self._get_device_id)
        else:
            raise NotSupportedError("Neither IPMI 2.0 nor IPMI 1.5 is supported by the BMC")



    def close_session(self):
        if self._stop_keep_alive:
            self._stop_keep_alive()

        if self._session.activated is False:
            log().debug('Session already closed')
            return

        log().debug('Close Session %s' % self._session)
        req = create_request_by_name('CloseSession')
        req.target = self.host_target
        req.session_id = self._session.sid
        rsp = self.send_and_receive(req)
        check_completion_code(rsp.completion_code)
        self._session.activated = False

#        self._q.join()

    def _inc_sequence_number(self):
        self.next_sequence_number = (self.next_sequence_number + 1) % 64

    def _send_and_receive_rmcp2(self, payload, payload_type):
        with self.transaction_lock:
            self._send_ipmi2_msg(payload, payload_type)
            return self._receive_ipmi2_msg()

    def _send_and_receive(self, target, lun, netfn, cmdid, payload):
        """Send and receive data using RMCP interface.

        target:
        lun:
        netfn:
        cmdid:
        raw_bytes: IPMI message payload as bytestring

        Returns the received data as array.
        """
        self._inc_sequence_number()

        header = IpmbHeaderReq()
        header.netfn = netfn
        header.rs_lun = lun
        header.rs_sa = target.ipmb_address
        header.rq_seq = self.next_sequence_number
        header.rq_lun = 0
        header.rq_sa = self.slave_address
        header.cmdid = cmdid

        # Bridge message
        if target.routing:
            tx_data = encode_bridged_message(target.routing, header, payload,
                                             self.next_sequence_number)
        else:
            tx_data = encode_ipmb_msg(header, payload)

        with self.transaction_lock:
            self._send_ipmi_msg(tx_data)

            received = False
            while received is False:
                if not self._q.empty():
                    rx_data = self._q.get()
                else:
                    rx_data = self._receive_ipmi_msg()

                if array('B', rx_data)[5] == constants.CMDID_SEND_MESSAGE:
                    rx_data = decode_bridged_message(rx_data)
                    if not rx_data:
                        # the forwarded reply is expected in the next packet
                        continue

                received = rx_filter(header, rx_data)

                if not received:
                    self._q.put(rx_data)

        return rx_data[6:-1]

    def send_and_receive_raw(self, target, lun, netfn, raw_bytes):
        """Interface function to send and receive raw message.

        target: IPMI target
        lun: logical unit number
        netfn: network function
        raw_bytes: RAW bytes as bytestring

        Returns the IPMI message response bytestring.
        """
        return self._send_and_receive(target=target,
                                      lun=lun,
                                      netfn=netfn,
                                      cmdid=array('B', raw_bytes)[0],
                                      payload=raw_bytes[1:])

    def send_and_receive(self, req):
        """Interface function to send and receive an IPMI message.

        target: IPMI target
        req: IPMI message request

        Returns the IPMI message response.
        """
        rx_data = self._send_and_receive(target=req.target,
                                         lun=req.lun,
                                         netfn=req.netfn,
                                         cmdid=req.cmdid,
                                         payload=encode_message(req))
        rsp = create_message(req.netfn + 1, req.cmdid, req.group_extension)
        decode_message(rsp, rx_data)
        return rsp

    def _wrap_ipmb(self, req):
        header = IpmbHeaderReq()
        header.netfn = req.netfn
        header.rs_lun = req.lun
        header.rs_sa = self.host_target.ipmb_address
        header.rq_seq = self.next_sequence_number
        header.rq_lun = 0
        header.rq_sa = self.slave_address
        header.cmdid = req.cmdid
        payload = encode_message(req)
        return encode_ipmb_msg(header, payload)


    def _unwrap_ipmb(self, rx_data):
        # Remove header and checksum
        # TODO: verify checksum
        return rx_data[6:-1]


    def send_and_receive_rmcp2(self, req, payload_type):
        """Interface function to send and receive an IPMI message.

        target: IPMI target
        req: IPMI message request

        Returns the IPMI message response.
        """
        tx_data = self._wrap_ipmb(req)
        rx_data = self._send_and_receive_rmcp2(tx_data, payload_type)
        rx_data = self._unwrap_ipmb(rx_data)
        rsp = create_message(req.netfn + 1, req.cmdid, req.group_extension)
        decode_message(rsp, rx_data)
        return rsp
