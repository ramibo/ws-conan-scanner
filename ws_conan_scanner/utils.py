import argparse

import csv
import io
import json
import logging
from datetime import datetime
from logging import handlers
import subprocess
from pathlib import Path

import requests
import sys
import ws_sdk
from ws_sdk.ws_utilities import PathType
from ws_conan_scanner._version import __tool_name__, __version__

# utilis module logger
utils_logger = logging.getLogger(f"{__tool_name__}.{__name__}")

# Todo [INFO] [2022-10-24 21:06:27,673 +0300]
LOGGING_FORMAT = logging.Formatter(fmt='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s.%(funcName)s:%(lineno)d] %(message)s',
                                   datefmt='%Y-%m-%d:%H:%M:%S')
# Environment variables
USER_KEY = 'userKey'
PROJECT_TOKEN = 'projectToken'
PRODUCT_TOKEN = 'productToken'
PROJECT_NAME = 'projectName'
PRODUCT_NAME = 'productName'
ORG_TOKEN = 'orgToken'
PROJECT_PATH = 'projectPath'
UNIFIED_AGENT_PATH = 'unifiedAgentPath'
CONAN_INSTALL_FOLDER = 'conanInstallFolder'
KEEP_CONAN_INSTALL_FOLDER_AFTER_RUN = 'keepConanInstallFolderAfterRun'
KEEP_CONAN_INSTALL_FOLDER_AFTER_RUN_DEFAULT = False
CHANGE_ORIGIN_LIBRARY = 'changeOriginLibrary'
CHANGE_ORIGIN_LIBRARY_DEFAULT = True
CONAN_RUN_PRE_STEP = 'conanRunPreStep'
CONAN_RUN_PRE_STEP_DEFAULT = False
INCLUDE_BUILD_REQUIRES_PACKAGES = 'includeBuildRequiresPackages'
INCLUDE_BUILD_REQUIRES_PACKAGES_DEFAULT = True
CONAN_PROFILE_NAME = 'conanProfileName'
CONAN_PROFILE_NAME_DEFAULT = 'default'
CONAN_MAIN_PACKAGE = 'conanMainPackage'
CONAN_MAIN_PACKAGE_DEFAULT = None
RESOLVE_CONAN_MAIN_PACKAGE = 'resolveConanMainPackage'
RESOLVE_CONAN_MAIN_PACKAGE_DEFAULT = True
WS_URL = 'wsUrl'
LOG_FILE_PATH = 'logFilePath'

DATE_TIME_NOW = datetime.now().strftime('%Y%m%d%H%M%S%f')
TEMP_FOLDER_PREFIX = 'conan_scanner_pre_process_'
ADDITIONAL_COMMANDS = 'additionalCommands'
ADDITIONAL_COMMANDS_DEFAULT = ''


# PROJECT_PARALLELISM_LEVEL = 'projectParallelismLevel'
# PROJECT_PARALLELISM_LEVEL_MAX_VALUE = 20
# PROJECT_PARALLELISM_LEVEL_DEFAULT_VALUE = 9
# PROJECT_PARALLELISM_LEVEL_RANGE = list(range(1, PROJECT_PARALLELISM_LEVEL_MAX_VALUE + 1))

def csv_to_json(csv_file_path):
    r_bytes = requests.get(csv_file_path).content
    r = r_bytes.decode('utf8')
    reader = csv.DictReader(io.StringIO(r))
    result_csv_reader = json.dumps(list(reader))
    json_result = json.loads(result_csv_reader)
    return json_result


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 'True', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'False', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def execute_command(command):
    try:
        utils_logger.info(f"Going to run the following command :\n{command}")
        output = subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT).decode()
        utils_logger.info(output)
        return output
    except subprocess.CalledProcessError as e:
        utils_logger.error(e.output.decode())


class LoggerFactory(object):
    _LOG = None

    @staticmethod
    def __create_logger():
        """
        A private method that interacts with the python
        logging module
        """
        hndlrs = []

        # Initialize the class variable with logger object
        LoggerFactory._LOG = logging.getLogger(name=__tool_name__)
        LoggerFactory._LOG.setLevel(level=logging.INFO)

        # create console stram handler with a higher log level
        sh = logging.StreamHandler(sys.stdout)
        sh.set_name(f'{__tool_name__}_sh')
        sh.setLevel(level=logging.DEBUG)
        hndlrs.append(sh)

        # # Add smtp handler to send mails upon error / exceptions
        # smtp_h = logging.handlers.SMTPHandler(mailhost=('smtp.gmail.com', 587),
        #                                       fromaddr='sender_mail_address',
        #                                       toaddrs=['receiver_mail_address1,receiver_mail_address1,'],
        #                                       subject='Subject',
        #                                       credentials=('user_mail', 'password'),
        #                                       secure=())
        #
        # smtp_h.set_name(f'{__tool_name__}_smtp_h')
        # smtp_h.setLevel(level=logging.ERROR)
        # hndlrs.append(smtp_h)

        for hndlr in hndlrs:
            # set format for each handler
            hndlr.setFormatter(fmt=LOGGING_FORMAT)

            # add the handlers to the logger
            LoggerFactory._LOG.addHandler(hdlr=hndlr)

        return LoggerFactory._LOG

    @staticmethod
    def get_logger():
        """
        A static method called by other modules to initialize logger in
        their own module
        """
        logger = LoggerFactory.__create_logger()

        # return the logger object
        return logger


class ConfigurationFactory(object):
    _CONF = None

    @staticmethod
    def __create_configuration():

        def get_args(arguments) -> dict:
            """Get configuration arguments"""

            parser = argparse.ArgumentParser(description='argument parser', add_help=True)

            required = parser.add_argument_group('required arguments')
            optional = parser.add_argument_group('optional arguments')
            ua_prod_proj = parser.add_argument_group('Unified Agent Product / Project')

            optional.add_argument('-q', "--" + ADDITIONAL_COMMANDS, help="List of additional commands to run", dest='additional_commands', required=False, default=ADDITIONAL_COMMANDS_DEFAULT, nargs='+')
            optional.add_argument('-s', "--" + KEEP_CONAN_INSTALL_FOLDER_AFTER_RUN, help="keep the install folder after run", dest='keep_conan_install_folder_after_run', required=False, default=KEEP_CONAN_INSTALL_FOLDER_AFTER_RUN_DEFAULT, type=str2bool)
            optional.add_argument('-b', "--" + INCLUDE_BUILD_REQUIRES_PACKAGES, help="If ture , list conan packages with conan info /path/to/conanfile --paths --dry-build.", type=str2bool, required=False, default=INCLUDE_BUILD_REQUIRES_PACKAGES_DEFAULT, dest='include_build_requires_packages')
            optional.add_argument('-p', "--" + CONAN_RUN_PRE_STEP, help="run conan install --build", dest='conan_run_pre_step', required=False, default=CONAN_RUN_PRE_STEP_DEFAULT, type=str2bool)
            optional.add_argument('-g', "--" + CHANGE_ORIGIN_LIBRARY, help="True will attempt to match libraries per package name and version", dest='change_origin_library', required=False, default=CHANGE_ORIGIN_LIBRARY_DEFAULT, type=str2bool)
            optional.add_argument('-f', "--" + CONAN_PROFILE_NAME, help="The name of the conan profile", dest='conan_profile_name', required=False, default=CONAN_PROFILE_NAME_DEFAULT)
            optional.add_argument('-m', "--" + CONAN_MAIN_PACKAGE, help="Include the package_name/package_version@user/channel of the project's conanfile package", dest='conan_main_package', required=False, default=CONAN_MAIN_PACKAGE_DEFAULT)
            optional.add_argument('-r', "--" + RESOLVE_CONAN_MAIN_PACKAGE, help="Retrieve and scan the source files of conanfile.py recipe main package via source method",
                                  dest='resolve_conan_main_package', required=False, default=RESOLVE_CONAN_MAIN_PACKAGE_DEFAULT, type=str2bool)
            required.add_argument('-u', '--' + WS_URL, help='The Mend organization url', required=True, dest='ws_url')
            required.add_argument('-k', '--' + USER_KEY, help='The admin user key', required=True, dest='user_key')
            required.add_argument('-t', '--' + ORG_TOKEN, help='The organization token', required=True, dest='org_token')
            ua_prod_proj.add_argument('--' + PRODUCT_TOKEN, help='The product token - Only required if projectToken is not defined.', required=False, dest='product_token')
            ua_prod_proj.add_argument('--' + PROJECT_TOKEN, help='The project token - Only required if projectName is not defined.', required=False, dest='project_token')
            ua_prod_proj.add_argument('--' + PRODUCT_NAME, help='The product name - Only required if projectToken is not defined.', required=False, dest='product_name')
            ua_prod_proj.add_argument('--' + PROJECT_NAME, help='The project name - Only required if projectToken is not defined.', required=False, dest='project_name')
            optional.add_argument('-l', '--' + LOG_FILE_PATH, help='Path to the conan_scanner_log_YYYYMMDDHHMMSS.log file', required=False, type=PathType(checked_type='dir'), dest='log_file_path')
            # parser.add_argument('-m', '--' + PROJECT_PARALLELISM_LEVEL, help='The number of threads to run with', required=not is_config_file, dest='project_parallelism_level', type=int, default=PROJECT_PARALLELISM_LEVEL_DEFAULT, choices=PROJECT_PARALLELISM_LEVEL_RANGE)
            required.add_argument('-d', "--" + PROJECT_PATH, help=f"The directory which contains the conanfile.txt / conanfile.py path", type=PathType(checked_type='dir'), required=True, dest='project_path')

            if '--' + PROJECT_PATH in args:
                project_p = arguments[arguments.index('--' + PROJECT_PATH) + 1]
            elif '-d' in args:
                project_p = arguments[arguments.index('-d') + 1]
            else:
                project_p = None
            optional.add_argument('-a', "--" + UNIFIED_AGENT_PATH, help=f"The directory which contains the Unified Agent", type=PathType(checked_type='dir'), required=False, default=project_p, dest='unified_agent_path')
            optional.add_argument('-i', "--" + CONAN_INSTALL_FOLDER, help=f"The folder in which the installation of packages outputs the generator files with the information of dependencies. Format: Y-m-d-H-M-S-f", type=PathType(checked_type='dir'), required=False, default=project_p, dest='conan_install_folder')

            args_dict = vars(parser.parse_args())

            return args_dict

        args = sys.argv[1:]
        if len(args) > 0:
            params_conf = get_args(args)

            # set log file from parent main logger
            if params_conf.get('log_file_path'):
                fh = logging.handlers.RotatingFileHandler(filename=Path(params_conf.get('log_file_path'),
                                                                        f'{__tool_name__}_log_{DATE_TIME_NOW}.log'),
                                                          backupCount=10)
                fh.set_name(f'{__tool_name__}_fh')
                fh.setLevel(level=logging.DEBUG)
                fh.setFormatter(fmt=LOGGING_FORMAT)
                logging.getLogger(__tool_name__).addHandler(hdlr=fh)

            utils_logger.info('Finished analyzing arguments.')

            # Defining a Config class Dynamically - https://www.freecodecamp.org/news/dynamic-class-definition-in-python-3e6f7d20a381/

            # class Config:
            #     def __init__(self, dictionary):
            #         for key, value in dictionary.items():
            #             setattr(self, key, value)
            #
            # ConfigurationFactory._CONF=Config(dictionary=params_conf)

            ConfigurationFactory._CONF = type('Config', (object,), params_conf)

            # Connection details
            utils_logger.info(f"ws connections details:\nwsURL: {ConfigurationFactory._CONF.ws_url}\n"
                              f"orgToken: {ConfigurationFactory._CONF.org_token}")

            # Set configuration for temp directory location which will contain dependencies source files.
            setattr(ConfigurationFactory._CONF, 'date_time_now', DATE_TIME_NOW)
            setattr(ConfigurationFactory._CONF, 'temp_dir', Path(ConfigurationFactory._CONF.conan_install_folder, TEMP_FOLDER_PREFIX + ConfigurationFactory._CONF.date_time_now))

            install_ref = ConfigurationFactory._CONF.project_path
            if ConfigurationFactory._CONF.conan_main_package:
                install_ref = ConfigurationFactory._CONF.conan_main_package
                if '@' not in install_ref:
                    install_ref = install_ref + '@'
            setattr(ConfigurationFactory._CONF, 'install_ref', install_ref)

            # Add connection attribute to the _CONF class variable
            setattr(ConfigurationFactory._CONF, 'ws_conn', ws_sdk.web.WSApp(url=ConfigurationFactory._CONF.ws_url,
                                                                            user_key=ConfigurationFactory._CONF.user_key,
                                                                            token=ConfigurationFactory._CONF.org_token,
                                                                            tool_details=(f"ps-{__tool_name__.replace('_', '-')}", __version__),
                                                                            timeout=3600))
            # Test connection
            setattr(ConfigurationFactory._CONF, 'ws_conn_details', ConfigurationFactory._CONF.ws_conn.get_organization_details())
            return ConfigurationFactory._CONF

    @staticmethod
    def get_configuration():
        config = ConfigurationFactory.__create_configuration()

        return config
