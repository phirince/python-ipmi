# Copyright (c) 2016  Kontron Europe GmbH
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

import hashlib
import hmac


class Session(object):
    AUTH_TYPE_NONE = 0x00
    AUTH_TYPE_MD2 = 0x01
    AUTH_TYPE_MD5 = 0x02
    AUTH_TYPE_PASSWORD = 0x04
    AUTH_TYPE_OEM = 0x05
    AUTH_TYPE_RMCP_PLUS = 0x06

    PRIV_LEVEL_USER = 2
    PRIV_LEVEL_OPERATOR = 3
    PRIV_LEVEL_ADMINISTRATOR = 4
    PRIV_LEVEL_OEM = 5

    session_id = None
    _interface = None
    _auth_type = AUTH_TYPE_NONE
    _auth_username = None
    _auth_password = None
    _rmcp_host = None
    _rmcp_port = None
    _serial_port = None
    _serial_baudrate = None
    _is_encrypted = False
    _is_authenticated = False
    _confidentiality_algorithm = None
    _integrity_algorithm = None
    _additional_encryption_keys = []

    def __init__(self):
        self.established = False
        self.sid = 0
        self.sequence_number = 0
        self.activated = False

    def _get_interface(self):
        try:
            return self._interface
        except AttributeError:
            raise RuntimeError('No interface has been set')

    def _set_interface(self, interface):
        self._interface = interface

    def increment_sequence_number(self):
        self.sequence_number += 1
        if self.sequence_number > 0xffffffff:
            self.sequence_number = 1

    def set_session_type_rmcp(self, host, port=623):
        self._rmcp_host = host
        self._rmcp_port = port

    @property
    def rmcp_host(self):
        return self._rmcp_host

    @property
    def rmcp_port(self):
        return self._rmcp_port

    def set_session_type_serial(self, port, baudrate):
        self._serial_port = port
        self._serial_baudrate = baudrate

    @property
    def serial_port(self):
        return self._serial_port

    @property
    def serial_baudrate(self):
        return self._serial_baudrate

    def _set_auth_type(self, auth_type):
        self._auth_type = auth_type

    def _get_auth_type(self):
        return self._auth_type

    def _set_is_encrypted(self, is_encrypted):
        self._is_encrypted = is_encrypted

    def _get_is_encrypted(self):
        return self._is_encrypted

    def _set_is_authenticated(self, is_authenticated):
        self._is_authenticated = is_authenticated

    def _get_is_authenticated(self):
        return self._is_authenticated

    def _get_integrity_algorithm(self):
        return self._integrity_algorithm

    def _set_integrity_algorithm(self, algo):
        self._integrity_algorithm = algo

    def _get_confidentiality_algorithm(self):
        return self._confidentiality_algorithm

    def _set_confidentiality_algorithm(self, algo):
        self._confidentiality_algorithm = algo

    @property
    def additional_encryption_keys(self):
        return self._additional_encryption_keys

    def generate_additional_encryption_keys(self, sik):
        k_1 = hmac.new(sik, b'\x01' * 20, hashlib.sha1).digest()
        k_2 = hmac.new(sik, b'\x02' * 20, hashlib.sha1).digest()
        self._additional_encryption_keys = [k_1, k_2]

    def set_auth_type_user(self, username, password):
        self._auth_type = self.AUTH_TYPE_PASSWORD
        self._auth_username = username
        self._auth_password = password

    @property
    def auth_username(self):
        return self._auth_username

    @property
    def auth_password(self):
        return self._auth_password

    def establish(self):
        if hasattr(self.interface, 'establish_session'):
            self.interface.establish_session(self)

    def close(self):
        if hasattr(self.interface, 'close_session'):
            self.interface.close_session()

    def rmcp_ping(self):
        if hasattr(self.interface, 'rmcp_ping'):
            self.interface.rmcp_ping()

    def __str__(self):
        string = 'Session:\n'
        string += '  ID: 0x%08x\n' % self.sid
        string += '  Seq: 0x%08x\n' % self.sequence_number
        string += '  Host: %s:%s\n' % (self._rmcp_host, self._rmcp_port)
        string += '  Auth.: %s\n' % self.auth_type
        string += '  User: %s\n' % self._auth_username
        string += '  Password: %s\n' % self._auth_password
        string += '\n'
        return string

    interface = property(_get_interface, _set_interface)
    auth_type = property(_get_auth_type, _set_auth_type)
    is_authenticated = property(_get_is_authenticated, _set_is_authenticated)
    is_encrypted = property(_get_is_encrypted, _set_is_encrypted)
    confidentiality_algorithm = property(_get_confidentiality_algorithm, _set_confidentiality_algorithm)
    integrity_algorithm = property(_get_integrity_algorithm, _set_integrity_algorithm)
