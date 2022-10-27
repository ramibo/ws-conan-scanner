import gc
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
import urllib3
import ws_sdk
import yaml
from ws_sdk.ws_constants import UAArchiveFiles
from ws_sdk.ws_utilities import convert_dict_list_to_dict

from ws_conan_scanner._version import __description__
from ws_conan_scanner.utils import csv_to_json, execute_command, LoggerFactory, ConfigurationFactory, TEMP_FOLDER_PREFIX

conan_profile = dict()

CONAN_FILE_TXT = 'conanfile.txt'
CONAN_FILE_PY = 'conanfile.py'
conan_file_types = [CONAN_FILE_TXT, CONAN_FILE_PY]

# Set Main logger
logger = LoggerFactory.get_logger()


def is_conan_installed():
    """ Validate conan is installed by retrieving the Conan home directory"""
    conan_version = execute_command('conan --version')

    if 'Conan version' in conan_version:
        logger.info(f"Conan identified - {conan_version} ")
    else:
        logger.error(f"Please check Conan is installed and configured properly ")
        sys.exit(1)


def map_conan_profile_values(conf):
    global conan_profile  # Todo change to parmter

    try:
        subprocess.check_output(f"conan profile show {conf.conan_profile_name}",
                                shell=True, stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError as e:
        logger.error(e.output.decode())
        logger.info(f"conan profile was not found: {conf.conan_profile_name}")
        sys.exit(1)

    params = ('os', 'os_build', 'arch', 'arch_build', 'compiler', 'compiler.runtime', 'compiler.version', 'build_type')
    conan_profile = {}
    for param in params:
        output = subprocess.Popen(f"conan profile get settings.{param} {conf.conan_profile_name}",
                                  shell=True, stdout=subprocess.PIPE, text=True).communicate()[0]
        conan_profile[param] = ''.join(output.partition('\n')[0:1])


def validate_project_manifest_file_exists(config):
    conan_file_exists = False
    config.is_conanfilepy = False
    logger.info(f"Checking for conanfile.")

    for con_f in conan_file_types:
        if os.path.exists(os.path.join(config.project_path, con_f)):
            conan_file_exists = True
            logger.info(f"The {con_f} manifest file exists in your environment.")

            config.is_conanfilepy = True if con_f == CONAN_FILE_PY else False

    if not conan_file_exists:
        logger.error(f"A supported conanfile was not found in {config.project_path}.")
        sys.exit(1)


def map_all_dependencies(config):
    """
    Function to list all dependencies with: conan info DIR_CONTAINING_CONANFILE --paths --dry-build --json TEMP_JSON_PATH
    :return:list
    """

    try:
        deps_json_file = os.path.join(config.temp_dir, 'deps.json')
        logger.info(f"Mapping project's dependencies to {deps_json_file}")

        dry_build = '--dry-build' if config.include_build_requires_packages else ''

        output = execute_command(f"conan info {config.install_ref} --paths {dry_build} --json {deps_json_file}")

        logger.info(f'\n{output}')  # Todo add print of deps.json

        with open(deps_json_file, encoding='utf-8') as f:
            deps_data = json.load(f)
        output_json = [x for x in deps_data if x.get('revision') is not None]  # filter items which have the revision tag
        return output_json
    except subprocess.CalledProcessError as e:
        logger.error(e.output.decode())
        logger.info("The conan scanner will stop due to a failure to")
        sys.exit(1)


def run_conan_install_command(config):
    """ Allocate the scanned project dependencies in the conanInstallFolder"""
    try:
        logger.info(f"conanRunPreStep={config.conan_run_pre_step}")
        execute_command(f"conan install {config.install_ref} --install-folder {config.temp_dir} --build --profile:build {config.conan_profile_name}")
        logger.info(f"installation completed , install folder : {config.temp_dir}")
    except subprocess.CalledProcessError as e:
        logger.error(e.output.decode())


def conan_cache_packages_source_folder_missing(conan_dependencies: list):
    missing_source = []
    for item in conan_dependencies:
        if os.path.exists(item.get('source_folder')):
            logger.info(f"Source folder exists for {item.get('reference')} at: {item.get('source_folder')}")
        else:
            logger.info(f"Source folder missing for {item.get('reference')} at: {item.get('source_folder')}")
            missing_source.append(item.get('reference'))
    return missing_source


def get_dependencies_from_download_source(config, source_folders_missing, conan_dependencies) -> list:
    """Download each dependency source files / archive to conanInstallFolder/YmdHMSf/package_name-package_version and returns a list of source files directories.
    :return: a list dictionaries {'package_name:package_version'}
    :rtype: list
    """
    config.directory = Path(config.temp_dir, "temp_deps")
    temp = '\n'.join(source_folders_missing)
    logger.info(f"The following packages source files are missing from the conan cache - will try to extract to {config.directory} :\n{temp}")

    deps_l_d = convert_dict_list_to_dict(lst=conan_dependencies, key_desc='reference')

    packages_l = []

    for item in source_folders_missing:
        export_folder = deps_l_d[item].get('export_folder')
        package_directory = os.path.join(config.directory, item.split('/')[0] + '-' + item.split('/')[1])  # replace  '/' with '-' to align with whitesource convention .
        pathlib.Path(package_directory).mkdir(parents=True, exist_ok=False)

        dependency_conan_data_yml = os.path.join(export_folder, 'conandata.yml')  # Check for conandata.yml file

        if os.path.isfile(os.path.join(export_folder, 'conanfile.py')):
            install_version = deps_l_d.get(item).get('reference')
            if '@' not in install_version:
                install_version = install_version + '@'
            conan_install_command = f"conan install --install-folder {package_directory} {export_folder} {install_version} --profile:build {config.conan_profile_name}"
            conan_source_command = f"conan source --source-folder {package_directory} --install-folder {package_directory} {export_folder}"

            try:
                execute_command(conan_install_command)
                execute_command(conan_source_command)

                packages_l.append(package_directory)

                # Get conandata.yml
                if os.path.isfile(os.path.join(package_directory, 'conandata.yml')):
                    deps_l_d.get(item)['conandata_yml'] = os.path.join(package_directory, 'conandata.yml')
                elif os.path.isfile(dependency_conan_data_yml):
                    deps_l_d.get(item)['conandata_yml'] = dependency_conan_data_yml

            except subprocess.CalledProcessError as e:
                logger.error(e.output.decode())

                if os.path.isfile(os.path.join(package_directory, 'conandata.yml')):

                    logger.info(f"Will try to get source from {os.path.join(package_directory, 'conandata.yml')} ")

                    package_directory_returned = download_source_package(source=os.path.join(package_directory, 'conandata.yml'),
                                                                         directory=package_directory,
                                                                         package_full_name=item)

                    packages_l.append(package_directory_returned)
                    deps_l_d.get(item)['conandata_yml'] = os.path.join(package_directory, 'conandata.yml')

                elif os.path.isfile(dependency_conan_data_yml):

                    logger.info(f"Will try to get source from {dependency_conan_data_yml} ")

                    package_directory_returned = download_source_package(source=dependency_conan_data_yml,
                                                                         directory=package_directory,
                                                                         package_full_name=item)

                    packages_l.append(package_directory_returned)
                    deps_l_d.get(item)['conandata_yml'] = dependency_conan_data_yml

                elif os.path.isfile(os.path.join(export_folder, 'conanfile.py')):  # creates conandata.yml from conanfile.py
                    logger.info(f"{item} conandata.yml is missing from {export_folder} - will try to get with conan source command")
                    try:
                        execute_command(conan_source_command)
                        package_directory_returned = download_source_package(source=package_directory,
                                                                             directory=package_directory,
                                                                             package_full_name=item)
                        packages_l.append(package_directory_returned)
                        deps_l_d.get(item)['conandata_yml'] = os.path.join(package_directory, 'conandata.yml')
                    except subprocess.CalledProcessError as e:
                        logger.error(e.output.decode())

                else:
                    logger.warning(f"{item} source files were not found")

    return packages_l  # Todo remove


def download_source_package(source, directory, package_full_name):
    general_text = f"Could not download source files for {package_full_name}"
    try:
        url = extract_url_from_conan_data_yml(source=source,
                                              package=package_full_name)
        if url:
            r = requests.get(url, allow_redirects=True, headers={'Cache-Control': 'no-cache'})
            with open(os.path.join(directory, os.path.basename(url)), 'wb') as b:
                b.write(r.content)
                logger.info(f"{package_full_name} source files were retrieved from {source} and saved at {directory} ")
                return directory
    except urllib3.exceptions.ProtocolError as e:
        logger.error(f'{general_text}\nGeneral requests error: ' + str(e.__traceback__))
    except requests.exceptions.ConnectionError as e:
        logger.error(f'{general_text}\nGeneral requests error: ' + e.response.text)
    except (FileNotFoundError, PermissionError, IsADirectoryError) as e:
        logger.warning(f"{general_text} as conandata.yml was not found or is not accessible: " + e.response.text)
    except requests.exceptions.URLRequired as e:
        logger.error(f'{general_text}\nThe url retrieved from conandata.yml is missing: ' + e.response.text)
    except requests.exceptions.InvalidURL as e:
        logger.error(f'{general_text}\nThe url retrieved from conandata.yml is Invalid: ' + e.response.text)
    except requests.exceptions.Timeout as e:
        logger.error(f'{general_text}\nGot requests Timeout: ' + e.response.text)
    except requests.exceptions.RequestException as e:
        logger.error(f'{general_text}\nGeneral requests error: ' + e.response.text)


def get_source_folders_list(source_folders_missing: list, conan_dependencies: list):
    source_folder_libs_l = []
    for item in conan_dependencies:
        if item.get('reference') not in source_folders_missing:
            source_folder_libs_l.append(item.get('source_folder'))
            item['conandata_yml'] = os.path.join(item.get('export_folder'), 'conandata.yml')
    return source_folder_libs_l


def scan_with_unified_agent(config, dirs_to_scan):
    dirs = []
    for item in dirs_to_scan:
        dirs.append(str(Path(item).absolute()))

    unified_agent = ws_sdk.web.WSClient(user_key=config.user_key,
                                        token=config.org_token,
                                        url=config.ws_url,
                                        ua_path=config.unified_agent_path)
    unified_agent.ua_conf.includes = '**/*.*'

    ws_exclude_hc = "**/ws_conan_scanned_*,jna-1649909383"
    ws_excludes_def = "**/*conan_export.tgz,**/*conan_package.tgz,**/*conanfile.py,**/node_modules,**/src/test,**/testdata,**/*sources.jar,**/*javadoc.jar"
    os.environ['WS_EXCLUDES'] = os.environ.get('WS_EXCLUDES') + ',' + ws_exclude_hc if os.environ.get('WS_EXCLUDES') is not None \
        else os.environ.get('WS_EXCLUDES', '') + ws_exclude_hc + ',' + ws_excludes_def

    unified_agent.ua_conf.archiveExtractionDepth = str(UAArchiveFiles.ARCHIVE_EXTRACTION_DEPTH_MAX)
    unified_agent.ua_conf.archiveIncludes = list(UAArchiveFiles.ALL_ARCHIVE_FILES)
    unified_agent.ua_conf.logLevel = 'debug'
    # unified_agent.ua_conf.scanPackageManager = True
    # #Todo - check for support in favor of https://docs.conan.io/en/latest/reference/conanfile/methods.html?highlight=system_requirements#system-requirements

    output = unified_agent.scan(scan_dir=dirs,
                                product_name=config.product_name,
                                product_token=config.product_token,
                                project_name=config.project_name,
                                project_token=config.project_token)
    logger.info(output[1])
    support_token = output[2]  # gets Support Token from scan output

    is_ua_scan_active = True
    while is_ua_scan_active:
        new_status = config.ws_conn.get_last_scan_process_status(support_token)
        logger.info(f"Scan data upload status :{new_status}")
        if new_status in ['UPDATED', 'FINISHED']:
            logger.info('scan upload completed')
            is_ua_scan_active = False
        elif new_status in ['UNKNOWN', 'FAILED']:
            logger.warning('scan failed to upload...exiting program')
            sys.exit(1)
        else:
            time.sleep(20.0)


def update_conandta_yml_download_url_from_ws_index(config, conan_deps):
    def sync_ws_org_with_conan_source_library_from_the_index(conf, index_package):
        from ws_sdk.ws_errors import WsSdkClientGenericError
        try:
            response = conf.ws_conn.call_ws_api(request_type='getSourceLibraryInfo',
                                                kv_dict={"owner": index_package.get('indexOwner'),
                                                         "name": index_package.get('name'),
                                                         "version": index_package.get('indexVersion'),
                                                         "host": index_package.get('repoUrl'),
                                                         "downloadLink": index_package.get('indexDownloadUrl')})
        except ws_sdk.ws_errors.WsSdkServerGenericError as e:
            # logger.warning(e)
            pass
        return response.get('keyUuid')

    index_download_links = convert_dict_list_to_dict(lst=csv_to_json('https://unified-agent.s3.amazonaws.com/conan_index_url_map.csv'), key_desc='conanDownloadUrl')
    for package in conan_deps:
        package['counter'] = 0  # done in favor of next step.
        source = package.get('conandata_yml')
        if source:
            url = extract_url_from_conan_data_yml(source=source,
                                                  package=package)
            if index_download_links.get(url):
                index_package_data = index_download_links.get(url)
                package.update({'conandata_yml_download_url': index_package_data.get('indexDownloadUrl')})

                # sync WS environemnt
                key_uuid = sync_ws_org_with_conan_source_library_from_the_index(config, index_package_data)
                package.update({'key_uuid': key_uuid})
            else:
                package.update({'conandata_yml_download_url': url})
        else:
            # Mainly for <package_name>/system ( no conandata.yml )
            package.update({'conandata_yml_download_url': None})


def get_project_inventory_dict_by_download_link(due_diligence: dict, inventory):
    for library in inventory:
        if due_diligence.get(library.get('filename')) is None:
            library['download_link'] = ''
        else:
            library['download_link'] = due_diligence[library['filename']].get('download_link')

    return convert_dict_list_to_dict(lst=inventory, key_desc='download_link')


def change_project_source_file_inventory_match(config, conan_deps):
    """changes source files mapping with changeOriginLibrary API"""

    def get_project_token_from_config(conf):
        if not conf.product_token:
            prod_token = conf.ws_conn.get_tokens_from_name(conf.product_name, token_type='product')[0]
        else:
            prod_token = conf.product_token

        if not config.project_token:
            projects_tokens = conf.ws_conn.get_scopes_from_name(conf.project_name, token_type='project')
            project_tokens_dict = convert_dict_list_to_dict(lst=projects_tokens, key_desc='product_token')
            proj_token = project_tokens_dict.get(prod_token).get('token')
        else:
            proj_token = conf.project_token
        return proj_token

    def process_project_due_diligence_report(conf, project_tok):
        project_due_diligence = conf.ws_conn.get_due_diligence(token=project_tok, report=False)
        for lib in project_due_diligence:
            if lib['library'][len(lib['library']) - 1] == '*':  # Remove astrix from the end of library name (occurs when licences number >1 )
                lib['library'] = lib['library'][:-1]

        return convert_dict_list_to_dict(project_due_diligence, key_desc='library')

    def prepare_project_source_files_to_remap(due_diligence_d: dict, sf_inventory):

        for source_file in sf_inventory:
            if 'Unmatched Source Files' not in source_file['library']['artifactId']:
                source_file['sc_counter'] = 0  # Debug
                source_file['source_lib_full_name'] = source_file['library']['artifactId'] + '-' + source_file['library']['version']
                # Add Download link to source file
                source_file['download_link'] = due_diligence_d.get(source_file['source_lib_full_name']).get('download_link')
            else:
                source_file['sc_counter'] = 0  # Debug
                source_file['source_lib_full_name'] = source_file['library']['artifactId'] + '-' + source_file['library']['version']

    def get_project_source_files_inventory_to_remap(deps, project_sf_inventory_to_remap_first_phase, project_inventory_d_by_download_link, org_n):
        project_sf_inventory_to_remap_second_phase = []
        libraries_key_uuid_and_sf_sha1 = defaultdict(list)

        # Todo project_sf_inventory_to_remap_first_phase_d_by_sf_path = convert_dict_list_to_dict(project_sf_inventory_to_remap_first_phase, 'path')

        missing_sf_counter_is_index_key_uuid = 0
        missing_sf_counter_is_not_index_key_uuid = 0
        for pkg in deps:
            for source_file in project_sf_inventory_to_remap_first_phase:
                if pkg['package_full_name'] in source_file['path'] or pkg['source_folder'] in source_file['path']:
                    if source_file.get('download_link') is not None and source_file.get('download_link') in str(project_inventory_d_by_download_link.get(pkg['conandata_yml_download_url'])):
                        pkg['counter'] += 1
                        source_file['accurate_match'] = True
                    elif pkg.get('key_uuid'):
                        source_file['need_to_remap'] = True
                        libraries_key_uuid_and_sf_sha1[json.dumps(pkg['key_uuid'])].append(source_file['sha1'])
                        missing_sf_counter_is_index_key_uuid += 1
                    else:
                        source_file['sc_counter'] += 1
                        project_sf_inventory_to_remap_second_phase.append(source_file)
                        missing_sf_counter_is_not_index_key_uuid += 1
            if pkg['counter'] > 0:
                logger.info(f"for {pkg['package_full_name']} conan package: {pkg['counter']} source files are mapped to the correct library ({project_inventory_d_by_download_link.get(pkg['conandata_yml_download_url'])['filename']}) in {org_name}")
            else:
                logger.info(f"for {pkg['package_full_name']} conan package: {pkg['counter']} source files are mapped to the correct library in {org_name}")
        missing_sf_counter = missing_sf_counter_is_index_key_uuid + missing_sf_counter_is_not_index_key_uuid
        logger.info(f"There are {missing_sf_counter} source files that can be re-mapped to the correct conan source library in {org_name}")
        return project_sf_inventory_to_remap_second_phase, libraries_key_uuid_and_sf_sha1

    def project_source_files_remap_first_phase(conf, libraries_key_uuid_and_sf_sha1, proj_token, org_n):
        from ws_sdk.ws_errors import WsSdkClientGenericError

        sha_ones_count = 0
        for key_uuid, sha_ones in libraries_key_uuid_and_sf_sha1.items():
            key_uuid = key_uuid.strip('"')
            try:
                conf.ws_conn.change_origin_of_source_lib(lib_uuid=key_uuid,
                                                         source_files_sha1=sha_ones,
                                                         user_comments=f"Source files changed by Mend conan scan_{conf.date_time_now}")
            except ws_sdk.ws_errors.WsSdkServerGenericError as e:
                # logger.warning(e)
                pass
            project_inventory_updated = conf.ws_conn.get_inventory(token=proj_token, with_dependencies=True, report=False)
            project_inventory_dict_by_key_uuid = convert_dict_list_to_dict(lst=project_inventory_updated, key_desc='keyUuid')
            logger.info(f"--{len(sha_ones)} source files were moved to {project_inventory_dict_by_key_uuid.get(key_uuid).get('filename')} library in {org_n}")
            sha_ones_count += len(sha_ones)

        logger.info(f"Total {sha_ones_count} source files were remapped to the correct libraries.")

    # this phase is to reduce source files which were matched based on path+download link but contained another package name in its path.
    def get_project_source_files_inventory_to_remap_third_phase(proj_sf_map_second_ph):
        project_sf_inventory_to_remap_third_phase = []
        for source_file in proj_sf_map_second_ph:
            if not source_file.get('accurate_match'):
                if not source_file.get('need_to_remap'):
                    if source_file['sc_counter'] < 2:
                        project_sf_inventory_to_remap_third_phase.append(source_file)
                    else:
                        pass

        return project_sf_inventory_to_remap_third_phase

    def get_packages_source_files_from_inventory_scan_results(proj_sf_map_third_ph, cn_deps):
        pkgs_and_sf_sha1 = defaultdict(list)

        for pkg in cn_deps:
            for source_file in proj_sf_map_third_ph:
                if pkg['package_full_name'] in source_file['path'] or pkg['source_folder'] in source_file['path']:
                    source_file['download_link'] = pkg.get('conandata_yml_download_url')  # Todo check if can be removed
                    pkgs_and_sf_sha1[json.dumps(pkg['package_full_name'])].append(source_file['sha1'])

        return pkgs_and_sf_sha1

    from ws_sdk.ws_errors import WsSdkClientGenericError
    org_name = config.ws_conn_details.get('orgName')
    logger.info(f"Start validating source files matching accuracy compared to the local conan cache in:\n"
                f"Mend Organization: {org_name}\n"
                f"Product name: {config.product_name}\n"
                f"Project name: {config.project_name}")

    # -=Filtering on project's source libraries download link compared with url from conandata.yml --> if it's the same , Mend source files matching was correct and no need to change.=-

    # Reducing source files which were mapped to the correct source library ( based on url from conandata.yml )
    project_token = get_project_token_from_config(conf=config)
    project_due_diligence_d_by_lib_name = process_project_due_diligence_report(conf=config,
                                                                               project_tok=project_token)
    project_source_files_inventory = config.ws_conn.get_source_file_inventory(report=False,
                                                                              token=project_token)

    prepare_project_source_files_to_remap(due_diligence_d=project_due_diligence_d_by_lib_name,
                                          sf_inventory=project_source_files_inventory)

    # get project inventory as it contain the keyUuid to be used later on
    project_inventory = config.ws_conn.get_inventory(token=project_token,
                                                     with_dependencies=True,
                                                     report=False)
    project_inventory_dict_by_download_link = get_project_inventory_dict_by_download_link(due_diligence=project_due_diligence_d_by_lib_name,
                                                                                          inventory=project_inventory)
    # get package full name and version
    for package in conan_deps:
        package.update({'package_full_name': package.get('reference').replace('/', '-'),
                        'name': package.get('reference').partition('/')[0],
                        'version': package.get('reference').partition('/')[2]})

    project_source_files_inventory_to_remap_second_phase, libraries_key_uuid_and_source_files_sha1 = get_project_source_files_inventory_to_remap(deps=conan_deps,
                                                                                                                                                 project_sf_inventory_to_remap_first_phase=project_source_files_inventory,
                                                                                                                                                 project_inventory_d_by_download_link=project_inventory_dict_by_download_link,
                                                                                                                                                 org_n=org_name)
    if len(libraries_key_uuid_and_source_files_sha1) > 0:
        project_source_files_remap_first_phase(conf=config,
                                               libraries_key_uuid_and_sf_sha1=libraries_key_uuid_and_source_files_sha1,
                                               proj_token=project_token,
                                               org_n=org_name)

    if len(project_source_files_inventory_to_remap_second_phase) > 0:
        project_source_files_inventory_to_remap_third_phase = get_project_source_files_inventory_to_remap_third_phase(project_source_files_inventory_to_remap_second_phase)

        remaining_conan_local_packages_and_source_files_sha1 = get_packages_source_files_from_inventory_scan_results(proj_sf_map_third_ph=project_source_files_inventory_to_remap_third_phase,
                                                                                                                     cn_deps=conan_deps)  # Todo check bzip2

        ####

        counter = 0
        packages_dict_by_package_full_name = convert_dict_list_to_dict(lst=conan_deps, key_desc='package_full_name')

        for package, sha1s in remaining_conan_local_packages_and_source_files_sha1.items():  # Todo - add threads
            no_match = True
            package = json.loads(package)
            if packages_dict_by_package_full_name[package].get('key_uuid'):
                logger.info(f"found a match for miss configured source files of {package}")
                try:
                    config.ws_conn.change_origin_of_source_lib(lib_uuid=packages_dict_by_package_full_name[package]['key_uuid'],
                                                               source_files_sha1=sha1s,
                                                               user_comments='Source files changed by Mend conan scan_' + config.date_time_now)
                except ws_sdk.ws_errors.WsSdkServerGenericError as e:
                    # logger.warning(e)
                    pass
                no_match = False
                counter += 1
                logger.info(f"--{counter}/{len(remaining_conan_local_packages_and_source_files_sha1)} libraries were matched "
                            f"( {len(sha1s)} mis-configured source files from {package} conan package were matched to Mend source library )")
            ####
            # Changing mis-mapped source files to optional library based on conan download url with global search
            else:
                logger.info(f"Trying match the remaining miss configured source files of {package} with global search")
                library_name = package.partition('-')[0]
                library_search_result = config.ws_conn.get_libraries(library_name)

                # Filtering results - only for 'Source Library'
                source_libraries = []
                for library in library_search_result:
                    if library['type'] == 'Source Library':
                        source_libraries.append(library)

                source_libraries_dict_from_search_by_download_link = convert_dict_list_to_dict(source_libraries, key_desc=str('url'))
                check_url = packages_dict_by_package_full_name[package]['conandata_yml_download_url']

                if source_libraries_dict_from_search_by_download_link.get(check_url):
                    library_key_uuid = source_libraries_dict_from_search_by_download_link[check_url].get('keyUuid')
                    logger.info(f"found a match by global search for miss configured source files of {package}")
                    try:
                        config.ws_conn.change_origin_of_source_lib(lib_uuid=library_key_uuid,
                                                                   source_files_sha1=sha1s,
                                                                   user_comments='Source files changed by Mend conan scan_' + config.date_time_now)
                    except ws_sdk.ws_errors.WsSdkServerGenericError as e:
                        # logger.warning(e)
                        pass
                    no_match = False
                    counter += 1
                    logger.info(f"--{counter}/{len(remaining_conan_local_packages_and_source_files_sha1)} libraries were matched "
                                f"( {len(sha1s)} mis-configured source files from {package} conan package were matched to "
                                f"{source_libraries_dict_from_search_by_download_link[check_url]['filename']} WS source library )")

                else:
                    logger.info(f"Match was not found by global search for miss configured source files of {package}")
                    logger.info(f"Trying match the remaining miss configured source files of {package} with name match")
                    project_source_files_inventory_to_remap_third_phase_dict = convert_dict_list_to_dict(lst=project_source_files_inventory_to_remap_third_phase, key_desc='sha1')

                    for library in project_inventory:
                        list1 = library.get('filename').lower()
                        list2 = [packages_dict_by_package_full_name.get(package).get('name'), packages_dict_by_package_full_name.get(package).get('version')]
                        result = all(elem in list1 for elem in list2)

                        if result and library.get('type') == 'SOURCE_LIBRARY':
                            logger.info(f"A match was found by name for conan pakcage {package} : to-->{library.get('filename')}")
                            library_key_uuid = library.get('keyUuid')
                            sha1s_final = []

                            for sha1 in sha1s:
                                if project_source_files_inventory_to_remap_third_phase_dict.get(sha1).get('source_lib_full_name') == library.get('filename'):
                                    logger.info(f"sha1: {sha1} is already mapped to {library.get('filename')}")
                                else:
                                    sha1s_final.append(sha1)
                            if len(sha1s_final) > 0:
                                try:
                                    config.ws_conn.change_origin_of_source_lib(lib_uuid=library_key_uuid,
                                                                               source_files_sha1=sha1s_final,
                                                                               user_comments='Source files changed by Mend conan scan_' + config.date_time_now)
                                except ws_sdk.ws_errors.WsSdkServerGenericError as e:
                                    # logger.warning(e)
                                    pass
                                no_match = False
                                logger.info(f"found a match for miss configured source files of {package}")
                                counter += 1
                                logger.info(f"--{counter}/{len(remaining_conan_local_packages_and_source_files_sha1)} libraries were matched ( {len(sha1s_final)} mis-configured source files from {package} conan package were matched to {library.get('filename')} WS source library )")
                            else:
                                no_match = False
                if no_match:
                    logger.info(f"Match was not found by name for miss configured source files of {package}")
                    logger.info(f"Did not find match for {package} package remaining source files.")


def extract_url_from_conan_data_yml(source, package):
    #  https://github.com/conan-io/hooks/pull/269 ,
    #  https://github.com/jgsogo/conan-center-index/blob/policy/patching-update/docs/conandata_yml_format.md
    try:
        with open(source) as a_yaml_file:
            parsed_yaml_file = yaml.load(a_yaml_file, Loader=yaml.FullLoader)
        temp = parsed_yaml_file.get('sources')
        for key, value in temp.items():
            url = value.get('url')
            if isinstance(url, dict) and url.get(conan_profile['os_build']):
                url = url.get(conan_profile['os_build'])
            if isinstance(url, dict) and url.get(conan_profile['arch_build']):
                url = url.get(conan_profile['arch_build'])
            if isinstance(url, list):
                url = url[-1]
            return url
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        logger.warning(f"Could not find {package} conandata.yml file")


def remove_previous_run_temp_folder(conf):
    """Remove temp folders from previous run of the connan scanner / UA"""

    def remove_folder(folder_path_pattern):
        try:
            for item in glob.iglob(folder_path_pattern, recursive=True):
                shutil.rmtree(item)
                logger.info(f"removed previous run folder :\n{item}")
        except OSError as e:
            logger.error("Error: %s - %s." % (e.filename, e.strerror))

    prefix_patterns = (str(Path(conf.conan_install_folder, TEMP_FOLDER_PREFIX + "*")),
                       str(Path(conf.unified_agent_path, 'ws-ua_*')),
                       str(Path(conf.unified_agent_path, 'WhiteSource-PlatformDependentFile_*')))

    for pattern in prefix_patterns:
        remove_folder(pattern)


def get_source_files_from_conan_main_package_recepie(config):
    if config.is_conanfilepy:
        execute_command(f"conan source {config.project_path} --source-folder {config.temp_dir}")


def run_additional_commands(config):

    for command in config.additional_commands:
        execute_command(command)



def main():
    # Get configuration from cli arguments
    config = ConfigurationFactory.get_configuration()

    # Remove temporary folders from previous run
    remove_previous_run_temp_folder(config)

    start_time = datetime.now()
    logger.info(f"Start running {__description__} on token {config.org_token}.")

    # Check if conan installed
    is_conan_installed()

    # Get Conan profile details
    map_conan_profile_values(config)

    #Run additionalCommands
    run_additional_commands(config)

    # Check for conanfile in the scanned project
    validate_project_manifest_file_exists(config)

    # Get the conan project dependencies
    conan_dependencies = map_all_dependencies(config)

    # Run Conan pre step
    if config.conan_run_pre_step:
        run_conan_install_command(config)

    # Get source files from the main package via conanfily.py source method
    if config.resolve_conan_main_package and not config.conan_main_package:
        get_source_files_from_conan_main_package_recepie(config)

    dirs_to_scan = [config.project_path]

    # Get conan packages which don't have source folder in the conan cache
    source_folders_missing = conan_cache_packages_source_folder_missing(conan_dependencies)

    if source_folders_missing:
        get_dependencies_from_download_source(config=config,
                                              source_folders_missing=source_folders_missing,
                                              conan_dependencies=conan_dependencies)

    # Get conan packages which have source folder in the conan cache
    source_from_conan_cache = get_source_folders_list(source_folders_missing=source_folders_missing,
                                                      conan_dependencies=conan_dependencies)
    for item in source_from_conan_cache:
        dirs_to_scan.append(item)

    # Adding {'conandata_yml_download_url':url} dictionary for each conan package and aligning with ws index convention
    update_conandta_yml_download_url_from_ws_index(config=config,
                                                   conan_deps=conan_dependencies)

    # Scan project
    scan_with_unified_agent(config=config,
                            dirs_to_scan=dirs_to_scan)

    # Change library for source files which were mapped incorrectly.
    if config.change_origin_library:
        change_project_source_file_inventory_match(config=config,
                                                   conan_deps=conan_dependencies)

    logger.info(f"Finished running {__description__}. Run time: {datetime.now() - start_time}")

    # Remove conan install folder
    if not config.keep_conan_install_folder_after_run:
        try:
            shutil.rmtree(config.temp_dir)
            logger.info(f"removed conanInstallFolder : {config.temp_dir}")
        except OSError as e:
            logger.error("Error: %s - %s." % (e.filename, e.strerror))
    else:
        temp_path = Path(config.project_path, 'ws_conan_scanned_' + config.date_time_now)
        logger.info(f"renaming {config.temp_dir} to {temp_path}")
        shutil.move(config.temp_dir, temp_path)


if __name__ == '__main__':
    gc.collect()
    main()
