#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2016 F5 Networks Inc.
# GNU General Public License v3.0 (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


ANSIBLE_METADATA = {'metadata_version': '1.1',
                    'status': ['preview'],
                    'supported_by': 'community'}

DOCUMENTATION = '''
---
module: bigip_device_license
short_description: Manage license installation and activation on BIG-IP devices
description:
  - Manage license installation and activation on a BIG-IP.
version_added: 2.6
options:
  license_key:
    description:
      - The registration key to use to license the BIG-IP.
      - This parameter is required if the C(state) is equal to C(present).
      - This parameter is not required when C(state) is C(absent) and will be
        ignored if it is provided.
  license_server:
    description:
      - The F5 license server to use when getting a license and validating a dossier.
      - This parameter is required if the C(state) is equal to C(present).
      - This parameter is not required when C(state) is C(absent) and will be
        ignored if it is provided.
    default: activate.f5.com
  state:
    description:
      - The state of the license on the system.
      - When C(present), only guarantees that a license is there.
      - When C(latest), ensures that the license is always valid.
      - When C(absent), removes the license on the system.
    default: present
    choices:
      - absent
      - present
  accept_eula:
    description:
      - Declares whether you accept the BIG-IP EULA or not. By default, this
        value is C(no). You must specifically declare that you have viewed and
        accepted the license. This module will not present you with that EULA
        though, so it is incumbent on you to read it.
      - The EULA can be found here; https://support.f5.com/csp/article/K12902.
      - This parameter is not required when C(state) is C(absent) and will be
        ignored if it is provided.
    type: bool
extends_documentation_fragment: f5
author:
  - Tim Rupp (@caphrim007)
'''

EXAMPLES = '''
- name: License BIG-IP using a key
  bigip_device_license:
    server: "lb.mydomain.com"
    user: "admin"
    password: "secret"
    license_key: "XXXXX-XXXXX-XXXXX-XXXXX-XXXXXXX"
  delegate_to: localhost

- name: Remove the license from the system
  bigip_device_license:
    server: "lb.mydomain.com"
    user: "admin"
    password: "secret"
    state: "absent"
  delegate_to: localhost
'''

RETURN = r'''
# only common fields returned
'''

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import open_url
from ansible.module_utils.six import iteritems

import re
import sys
import time
import xml.etree.ElementTree

try:
    from library.module_utils.network.f5.bigip import HAS_F5SDK
    from library.module_utils.network.f5.bigip import F5Client
    from library.module_utils.network.f5.common import F5ModuleError
    from library.module_utils.network.f5.common import AnsibleF5Parameters
    from library.module_utils.network.f5.common import cleanup_tokens
    from library.module_utils.network.f5.common import fq_name
    from library.module_utils.network.f5.common import f5_argument_spec
    from library.module_utils.network.f5.icontrol import iControlRestSession
    from library.module_utils.network.f5.icontrol import Request
    from library.module_utils.network.f5.icontrol import Response
    try:
        from library.module_utils.network.f5.common import iControlUnexpectedHTTPError
        from f5.sdk_exception import UtilError
    except ImportError:
        HAS_F5SDK = False
except ImportError:
    from ansible.module_utils.network.f5.bigip import HAS_F5SDK
    from ansible.module_utils.network.f5.bigip import F5Client
    from ansible.module_utils.network.f5.common import F5ModuleError
    from ansible.module_utils.network.f5.common import AnsibleF5Parameters
    from ansible.module_utils.network.f5.common import cleanup_tokens
    from ansible.module_utils.network.f5.common import fq_name
    from ansible.module_utils.network.f5.common import f5_argument_spec
    from ansible.module_utils.network.f5.icontrol import iControlRestSession
    from ansible.module_utils.network.f5.icontrol import Request
    from ansible.module_utils.network.f5.icontrol import Response
    try:
        from ansible.module_utils.network.f5.common import iControlUnexpectedHTTPError
        from f5.sdk_exception import UtilError
    except ImportError:
        HAS_F5SDK = False


class LicenseXmlParser(object):
    def __init__(self, content=None):
        self.raw_content = content
        try:
            self.content = xml.etree.ElementTree.fromstring(content)
        except xml.etree.ElementTree.ParseError as ex:
            raise F5ModuleError("Provided XML payload is invalid. Received '{0}'.".format(str(ex)))

    @property
    def namespaces(self):
        result = {
            'xsi': 'http://www.w3.org/2001/XMLSchema-instance'
        }
        return result

    @property
    def eula(self):
        try:
            root = self.content.findall('.//eula', self.namespaces)
            return root[0].text
        except Exception:
            return None

    @property
    def license(self):
        try:
            root = self.content.findall('.//license', self.namespaces)
            return root[0].text
        except Exception:
            return None

    def find_element(self, value):
        root = self.content.findall('.//multiRef', self.namespaces)
        if len(root) == 0:
            return None
        for elem in root:
            for k, v in iteritems(elem.attrib):
                if value in v:
                    return elem

    @property
    def state(self):
        elem = self.find_element('TransactionState')
        if elem is not None:
            return elem.text

    @property
    def fault_number(self):
        fault = self.get_fault()
        return fault.get('faultNumber', None)

    @property
    def fault_text(self):
        fault = self.get_fault()
        return fault.get('faultText', None)

    def get_fault(self):
        result = dict()

        root = self.find_element('LicensingFault')
        if root is None:
            return result

        for elem in root:
            if elem.tag == 'faultNumber':
                result['faultNumber'] = int(elem.text)
            elif elem.tag == 'faultText':
                tmp = elem.attrib.get('{http://www.w3.org/2001/XMLSchema-instance}nil', None)
                if tmp == 'true':
                    result['faultText'] = None
                else:
                    result['faultText'] = elem.text
        if 'faultNumber' not in result:
            result['faultNumber'] = None
        return result

    def json(self):
        result = dict(
            eula=self.eula or None,
            license=self.license or None,
            state=self.state or None,
            fault_number=self.fault_number,
            fault_text=self.fault_text or None
        )
        return result


class Parameters(AnsibleF5Parameters):
    api_map = {
        'licenseEndDateTime': 'license_end_date_time'
    }

    api_attributes = [

    ]

    returnables = [

    ]

    updatables = [

    ]


class ApiParameters(Parameters):
    pass


class ModuleParameters(Parameters):
    @property
    def license_options(self):
        result = dict(
            eula=self.eula or '',
            email=self.email or '',
            first_name=self.first_name or '',
            last_name=self.last_name or '',
            company=self.company or '',
            phone=self.phone or '',
            job_title=self.job_title or '',
            address=self.address or '',
            city=self.city or '',
            state=self.state or '',
            postal_code=self.postal_code or '',
            country=self.country or ''
        )
        return result

    @property
    def license_url(self):
        result = 'https://{0}/license/services/urn:com.f5.license.v5b.ActivationService'.format(self.license_server)
        return result

    @property
    def license_envelope(self):
        result = """<?xml version="1.0" encoding="UTF-8"?>
        <SOAP-ENV:Envelope xmlns:ns3="http://www.w3.org/2001/XMLSchema"
                           xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/"
                           xmlns:ns0="http://schemas.xmlsoap.org/soap/encoding/"
                           xmlns:ns1="https://{0}/license/services/urn:com.f5.license.v5b.ActivationService"
                           xmlns:ns2="http://schemas.xmlsoap.org/soap/envelope/"
                           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                           xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"
                           SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
          <SOAP-ENV:Header/>
          <ns2:Body>
            <ns1:getLicense>
              <dossier xsi:type="ns3:string">{1}</dossier>
              <eula xsi:type="ns3:string">{eula}</eula>
              <email xsi:type="ns3:string">{email}</email>
              <firstName xsi:type="ns3:string">{first_name}</firstName>
              <lastName xsi:type="ns3:string">{last_name}</lastName>
              <companyName xsi:type="ns3:string">{company}</companyName>
              <phone xsi:type="ns3:string">{phone}</phone>
              <jobTitle xsi:type="ns3:string">{job_title}</jobTitle>
              <address xsi:type="ns3:string">{address}</address>
              <city xsi:type="ns3:string">{city}</city>
              <stateProvince xsi:type="ns3:string">{state}</stateProvince>
              <postalCode xsi:type="ns3:string">{postal_code}</postalCode>
              <country xsi:type="ns3:string">{country}</country>
            </ns1:getLicense>
          </ns2:Body>
        </SOAP-ENV:Envelope>"""
        result = result.format(self.license_server, self.dossier, **self.license_options)
        return result


class Changes(Parameters):
    def to_return(self):
        result = {}
        try:
            for returnable in self.returnables:
                result[returnable] = getattr(self, returnable)
            result = self._filter_params(result)
        except Exception:
            pass
        return result


class UsableChanges(Changes):
    pass


class ReportableChanges(Changes):
    pass


class Difference(object):
    def __init__(self, want, have=None):
        self.want = want
        self.have = have

    def compare(self, param):
        try:
            result = getattr(self, param)
            return result
        except AttributeError:
            return self.__default(param)

    def __default(self, param):
        attr1 = getattr(self.want, param)
        try:
            attr2 = getattr(self.have, param)
            if attr1 != attr2:
                return attr1
        except AttributeError:
            return attr1


class ModuleManager(object):
    def __init__(self, *args, **kwargs):
        self.module = kwargs.get('module', None)
        self.client = kwargs.get('client', None)
        self.want = ModuleParameters(params=self.module.params, client=self.client)
        self.have = ApiParameters(client=self.client)
        self.changes = UsableChanges()
        self.escape_patterns = r'([$"' + "'])"

    def _set_changed_options(self):
        changed = {}
        for key in Parameters.returnables:
            if getattr(self.want, key) is not None:
                changed[key] = getattr(self.want, key)
        if changed:
            self.changes = UsableChanges(params=changed)

    def _update_changed_options(self):
        diff = Difference(self.want, self.have)
        updatables = Parameters.updatables
        changed = dict()
        for k in updatables:
            change = diff.compare(k)
            if change is None:
                continue
            else:
                if isinstance(change, dict):
                    changed.update(change)
                else:
                    changed[k] = change
        if changed:
            self.changes = UsableChanges(params=changed)
            return True
        return False

    def should_update(self):
        result = self._update_changed_options()
        if result:
            return True
        return False

    def exec_module(self):
        changed = False
        result = dict()
        state = self.want.state

        try:
            if state == "present":
                changed = self.present()
            elif state == "absent":
                changed = self.absent()
        except iControlUnexpectedHTTPError as e:
            raise F5ModuleError(str(e))

        reportable = ReportableChanges(params=self.changes.to_return())
        changes = reportable.to_return()
        result.update(**changes)
        result.update(dict(changed=changed))
        self._announce_deprecations(result)
        return result

    def _announce_deprecations(self, result):
        warnings = result.pop('__warnings', [])
        for warning in warnings:
            self.client.module.deprecate(
                msg=warning['msg'],
                version=warning['version']
            )

    def present(self):
        if self.exists():
            return False
        else:
            return self.create()

    def remove(self):
        if self.module.check_mode:
            return True
        self.remove_from_device()
        if self.exists():
            raise F5ModuleError("Failed to delete the resource.")
        return True

    def read_dossier_from_device(self):
        result = self.client.api.tm.util.get_dossier.exec_cmd(
            'run', utilCmdArgs='-b {0}'.format(self.want.license_key)
        )
        try:
            return result.commandResult
        except Exception:
            return None

    def generate_license_from_remote(self):
        mgmt = iControlRestSession()
        mgmt.verify = False
        mgmt.headers = {
            'SOAPAction': '""',
            'Content-Type': 'text/xml; charset=utf-8',
        }

        for x in range(0, 10):
            try:
                resp = mgmt.post(self.want.license_url, data=self.want.license_envelope)
            except Exception as ex:
                continue

            try:
                resp = LicenseXmlParser(content=resp._content)
                result = resp.json()
            except F5ModuleError as ex:
                # This error occurs when there is a problem with the license server and it
                # starts returning invalid XML (like if they upgraded something and the server
                # is redirecting improperly.
                #
                # There's no way to recover from this error except by notifying F5 that there
                # is an issue with the license server.
                raise
            except Exception as ex:
                continue

            if result['state'] == 'EULA_REQUIRED':
                self.want.update({'eula': result['eula']})
                continue
            if result['state'] == 'LICENSE_RETURNED':
                return result
            elif result['state'] == 'EMAIL_REQUIRED':
                raise F5ModuleError("Email must be provided")
            elif result['state'] == 'CONTACT_INFO_REQUIRED':
                raise F5ModuleError("Contact info must be provided")
            else:
                raise F5ModuleError(result['fault_text'])

    def create(self):
        self._set_changed_options()
        if not self.want.accept_eula:
            raise F5ModuleError(
                "You must read and accept the product EULA to license the box."
            )
        if self.module.check_mode:
            return True

        dossier = self.read_dossier_from_device()
        if dossier:
            self.want.update({'dossier': dossier})
        else:
            raise F5ModuleError("Dossier not generated.")

        self.create_on_device()
        self.wait_for_mcpd()
        if not self.exists():
            raise F5ModuleError(
                "Failed to license the device."
            )
        return True

    def absent(self):
        if self.any_license_exists():
            self.remove()
            self.wait_for_mcpd()
            if self.exists():
                raise F5ModuleError(
                    "Failed to remove the license from the device."
                )
            return True
        return False

    def exists(self):
        resource = self.client.api.tm.shared.licensing.registration.load()
        try:
            if resource.registrationKey == self.want.license_key:
                return True
        except Exception:
            pass
        return False

    def wait_for_mcpd(self):
        nops = 0

        # Sleep a little to let mcpd settle and begin properly
        time.sleep(5)

        while nops < 4:
            try:
                if self._is_mcpd_ready_on_device():
                    nops += 1
                else:
                    nops = 0
            except Exception as ex:
                pass
            time.sleep(5)

    def _is_mcpd_ready_on_device(self):
        try:
            output = self.client.api.tm.util.bash.exec_cmd(
                'run',
                utilCmdArgs='-c "tmsh show sys mcp-state | grep running"'
            )
            if hasattr(output, 'commandResult'):
                return True
        except Exception as ex:
            pass
        return False

    def any_license_exists(self):
        resource = self.client.api.tm.shared.licensing.registration.load()
        try:
            if resource.registrationKey is not None:
                return True
        except Exception:
            pass
        return False

    def create_on_device(self):
        license = self.generate_license_from_remote()
        if license is None:
            raise F5ModuleError(
                "Failed to generate license from F5 activation servers."
            )
        result = self.upload_license_to_device(license)
        if not result:
            raise F5ModuleError(
                "Failed to install license on device."
            )
        result = self.upload_eula_to_device(license)
        if not result:
            raise F5ModuleError(
                "Failed to upload EULA file to device."
            )
        result = self.reload_license()
        if not result:
            raise F5ModuleError(
                "Failed to reload license configuration."
            )

    def upload_license_to_device(self, license):
        license_payload = re.sub(self.escape_patterns, r'\\\1', license['license'])
        command = """cat > /config/bigip.license <<EOF\n{0}\nEOF""".format(license_payload)
        command = '-c "{0}"'.format(command)
        self.client.api.tm.util.bash.exec_cmd('run', utilCmdArgs=command)
        return True

    def upload_eula_to_device(self, license):
        eula_payload = re.sub(self.escape_patterns, r'\\\1', license['eula'])
        command = """cat > /LICENSE.F5 <<EOF\n{0}\nEOF""".format(eula_payload)
        command = '-c "{0}"'.format(command)
        self.client.api.tm.util.bash.exec_cmd('run', utilCmdArgs=command)
        return True

    def reload_license(self):
        command = '-c "{0}"'.format("/usr/bin/reloadlic")
        self.client.api.tm.util.bash.exec_cmd('run', utilCmdArgs=command)
        return True

    def remove_from_device(self):
        result = self.remove_license_from_device()
        if not result:
            raise F5ModuleError(
                "Failed to remove license from device."
            )
        result = self.remove_eula_from_device()
        if not result:
            raise F5ModuleError(
                "Failed to remove EULA file from device."
            )
        result = self.reload_license()
        if not result:
            raise F5ModuleError(
                "Failed to reload the empty license configuration."
            )

    def remove_license_from_device(self):
        # UtilError should be raised when non-existent file is mentioned
        try:
            self.client.api.tm.util.unix_rm.exec_cmd(
                'run', utilCmdArgs='/config/bigip.license'
            )
        except UtilError as ex:
            if 'No such file or directory' in str(ex):
                return True
            else:
                raise F5ModuleError(str(ex))
        return True

    def remove_eula_from_device(self):
        # UtilError should be raised when non-existent file is mentioned
        try:
            self.client.api.tm.util.unix_rm.exec_cmd('run', utilCmdArgs='/LICENSE.F5')
        except UtilError as ex:
            if 'No such file or directory' in str(ex):
                return True
            else:
                raise F5ModuleError(str(ex))
        return True


class ArgumentSpec(object):
    def __init__(self):
        self.supports_check_mode = True
        argument_spec = dict(
            license_key=dict(),
            license_server=dict(
                default='activate.f5.com'
            ),
            state=dict(
                choices=['absent', 'present'],
                default='present'
            ),
            accept_eula=dict(
                type='bool',
                default='no'
            )
        )
        self.argument_spec = {}
        self.argument_spec.update(f5_argument_spec)
        self.argument_spec.update(argument_spec)
        self.required_if = [
            ['state', 'present', ['accept_eula', 'license_key']]
        ]


def main():
    spec = ArgumentSpec()

    module = AnsibleModule(
        argument_spec=spec.argument_spec,
        supports_check_mode=spec.supports_check_mode
    )
    if not HAS_F5SDK:
        module.fail_json(msg="The python f5-sdk module is required")

    try:
        client = F5Client(**module.params)
        mm = ModuleManager(module=module, client=client)
        results = mm.exec_module()
        cleanup_tokens(client)
        module.exit_json(**results)
    except F5ModuleError as ex:
        cleanup_tokens(client)
        module.fail_json(msg=str(ex))


if __name__ == '__main__':
    main()
