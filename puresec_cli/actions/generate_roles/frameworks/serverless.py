from puresec_cli.actions.generate_roles.frameworks.base import Base
from puresec_cli.utils import eprint, input_query, capitalize
from ruamel.yaml import YAML
from subprocess import Popen, PIPE, STDOUT
from tempfile import TemporaryDirectory
from zipfile import ZipFile, BadZipFile
import json
import os
import re

yaml = YAML() # using non-breaking yaml now

class ServerlessFramework(Base):
    def __init__(self, path, config, executable, yes=False):
        if not executable:
            executable = 'serverless' # from environment (PATH)

        super().__init__(
            path, config,
            executable=executable,
            yes=yes
        )

    def __exit__(self, type, value, traceback):
        super().__exit__(type, value, traceback)

        if hasattr(self, '_serverless_package'):
            self._serverless_package.cleanup()

    def result(self, provider):
        permissions = provider.permissions
        if not permissions:
            return

        # dumping roles
        result_path = os.path.join(self.path, 'puresec-roles.yml')
        if not self.yes and os.path.exists(result_path):
            if not input_query("Roles file already exists, overwrite?"):
                raise SystemExit(1)

        with open(result_path, 'w') as f:
            yaml.dump(provider.roles, f)

        # modifying serverless.yml
        config_path = os.path.join(self.path, "serverless.yml")
        with open(config_path, 'r') as f:
            config = yaml.load(f)

        new_resources = config.setdefault('resources', {}).setdefault('Resources', {})
        new_roles = set()

        # adding roles
        config.setdefault('custom', {})['puresec_roles'] = "${file(puresec-roles.yml)}"
        for name in permissions.keys():
            role = "puresec{}Role".format(capitalize(name))
            new_roles.add(role)
            new_resources[role] = "${{self:custom.puresec_roles.PureSec{}Role}}".format(capitalize(name))

        # referencing
        if self.yes or input_query("Reference functions to new roles?"):
            for name in permissions.keys():
                config['functions'][name]['role'] = "puresec{}Role".format(capitalize(name))

        # remove old
        if self.yes or input_query("Remove old roles?"):
            old_resources = self._serverless_config['service'].get('resources', {}).get('Resources', {})
            # default role
            old_roles = []
            if 'iamRoleStatements' in config.get('provider', ()):
                old_roles.append("default service-level role")
            # roles assumed for lambda
            for resource_id, resource_config in old_resources.items():
                if resource_config['Type'] == 'AWS::IAM::Role':
                    # meh
                    if 'lambda.amazonaws.com' in str(resource_config.get('Properties', {}).get('AssumeRolePolicyDocument')):
                        if resource_id not in new_roles and resource_id in new_resources:
                            old_roles.append(resource_config.get('Properties', {}).get('RoleName', resource_id))
            # removing
            if old_roles:
                old_roles = "\n".join("- {}".format(role) for role in old_roles)
                if self.yes or input_query("These are the roles that would be removed:\n{}\nAre you sure?".format(old_roles)):
                    if 'iamRoleStatements' in config.get('provider', ()):
                        del config['provider']['iamRoleStatements']
                    for resource_id, resource_config in old_resources.items():
                        if resource_config['Type'] == 'AWS::IAM::Role':
                            # meh
                            if 'lambda.amazonaws.com' in str(resource_config.get('Properties', {}).get('AssumeRolePolicyDocument')):
                                if resource_id not in new_roles and resource_id in new_resources:
                                    del new_resources[resource_id]

        if not new_resources and 'resources' in config:
            del config['resources']

        with open(config_path, 'w') as f:
            yaml.dump(config, f)

    def _package(self):
        """
        >>> from tests.mock import Mock
        >>> mock = Mock(__name__)
        >>> mock.mock(None, 'eprint')

        >>> ServerlessFramework("path/to/project", {}, executable="ls")._package()
        Traceback (most recent call last):
        SystemExit: -1
        >>> mock.calls_for('eprint')
        'error: could not find serverless config in: {}', 'path/to/project/serverless.yml'
        """

        if not hasattr(self, '_serverless_package'):
            # sanity check so that we know FileNotFoundError later means Serverless is not installed
            serverless_config_path = os.path.join(self.path, "serverless.yml")
            if not os.path.exists(serverless_config_path):
                eprint("error: could not find serverless config in: {}", serverless_config_path)
                raise SystemExit(-1)

            self._serverless_package = TemporaryDirectory(prefix="puresec-")

            try:
                process = Popen([self.executable, 'package', '--package', self._serverless_package.name], cwd=self.path, stdout=PIPE, stderr=STDOUT)
            except FileNotFoundError:
                eprint("error: serverless framework not installed, try using --framework-path")
                raise SystemExit(-1)

            result = process.wait()
            if result != 0:
                output, _ = process.communicate()
                eprint("error: serverless package failed:\n{}", output.decode())
                raise SystemExit(result)

    @property
    def _serverless_config(self):
        """
        >>> from pprint import pprint
        >>> from collections import namedtuple
        >>> from tests.mock import Mock
        >>> mock = Mock(__name__)
        >>> mock.mock(None, 'eprint')

        >>> TemporaryDirectory = namedtuple('TemporaryDirectory', ('name',))

        >>> framework = ServerlessFramework("path/to/project", {}, executable="ls")
        >>> framework._package = lambda: None

        >>> framework._serverless_package = TemporaryDirectory('/tmp/package')
        >>> framework._serverless_config
        Traceback (most recent call last):
        SystemExit: -1
        >>> mock.calls_for('eprint')
        'error: serverless package did not create serverless-state.json'

        >>> with mock.open('/tmp/package/serverless-state.json', 'w') as f:
        ...     f.write('invalid') and None
        >>> framework._serverless_config
        Traceback (most recent call last):
        SystemExit: -1
        >>> mock.calls_for('eprint')
        'error: invalid serverless-state.json:\\n{}', ValueError('Expecting value: line 1 column 1 (char 0)',)

        >>> with mock.open('/tmp/package/serverless-state.json', 'w') as f:
        ...     f.write('{ "x": { "y": 1 }, "z": 2 }') and None
        >>> pprint(framework._serverless_config)
        {'x': {'y': 1}, 'z': 2}
        """

        if hasattr(self, '_serverless_config_cache'):
            return self._serverless_config_cache

        self._package()
        try:
            serverless_config = open(os.path.join(self._serverless_package.name, 'serverless-state.json'), 'r', errors='replace')
        except FileNotFoundError:
            eprint("error: serverless package did not create serverless-state.json")
            raise SystemExit(-1)

        with serverless_config:
            try:
                self._serverless_config_cache = json.load(serverless_config)
            except ValueError as e:
                eprint("error: invalid serverless-state.json:\n{}", e)
                raise SystemExit(-1)

        return self._serverless_config_cache

    def get_provider_name(self):
        return self._serverless_config['service']['provider']['name']

    def get_resource_template(self):
        self._package()
        return os.path.join(self._serverless_package.name, 'cloudformation-template-update-stack.json')

    def get_default_profile(self):
        return self._serverless_config['service']['provider'].get('profile')

    def get_default_region(self):
        return self._serverless_config['service']['provider'].get('region')

    def get_function_name(self, provider_function_name):
        """
        >>> from tests.mock import Mock
        >>> mock = Mock(__name__)
        >>> mock.mock(None, 'eprint')
        >>> framework = ServerlessFramework("path/to/project", {}, executable="ls")

        >>> framework._serverless_config_cache = {'service': {'functions': {'otherFunction': {'name': 'other-function'}}}}
        >>> framework.get_function_name('function-name')
        Traceback (most recent call last):
        SystemExit: -1
        >>> mock.calls_for('eprint')
        "error: could not find Serverless name for function: '{}'", 'function-name'

        >>> framework._serverless_config_cache = {'service': {'functions': {'functionName': {'name': 'function-name'}}}}
        >>> framework.get_function_name('function-name')
        'functionName'
        """

        for name, function_config in self._serverless_config['service'].get('functions', {}).items():
            if function_config['name'] == provider_function_name:
                return name

        eprint("error: could not find Serverless name for function: '{}'", provider_function_name)
        raise SystemExit(-1)

    def get_function_root(self, name):
        self._package()

        package_name = self._get_function_package_name(name)
        function_root = os.path.join(self._serverless_package.name, package_name)
        if os.path.exists(function_root):
            return function_root

        try:
            zipfile = ZipFile(os.path.join(self._serverless_package.name, "{}.zip".format(package_name)), 'r')
        except FileNotFoundError:
            eprint("error: serverless package did not create a function zip for '{}'", name)
            raise SystemExit(2)
        except BadZipFile:
            eprint("error: serverless package did not create a valid function zip for '{}'", name)
            raise SystemExit(2)

        with zipfile:
            zipfile.extractall(function_root)
        return function_root

    def _get_function_package_name(self, name):
        """
        >>> from tests.mock import Mock
        >>> mock = Mock(__name__)
        >>> mock.mock(None, 'eprint')
        >>> framework = ServerlessFramework("path/to/project", {}, executable="ls")

        >>> framework._serverless_config_cache = {'service': {'service': "serviceName"}}
        >>> framework._get_function_package_name('functionName')
        'serviceName'

        >>> framework._serverless_config_cache = {'service': {'service': "serviceName"}, 'package': {'individually': True}}
        >>> framework._get_function_package_name('functionName')
        'functionName'
        """

        if not self._serverless_config.get('package', {}).get('individually', False):
            return self._serverless_config['service']['service']
        else:
            return name

Framework = ServerlessFramework
