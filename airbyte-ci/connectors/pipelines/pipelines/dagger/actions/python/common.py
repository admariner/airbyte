#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import re
from pathlib import Path
from typing import List, Optional, Sequence

from click import UsageError
from dagger import Container, Directory

from pipelines import hacks
from pipelines.airbyte_ci.connectors.context import ConnectorContext, PipelineContext
from pipelines.consts import PATH_TO_LOCAL_CDK
from pipelines.dagger.containers.python import with_pip_cache, with_poetry_cache, with_python_base
from pipelines.helpers.utils import check_path_in_workdir, get_file_contents, raise_if_not_user


def with_python_package(
    context: PipelineContext,
    python_environment: Container,
    package_source_code_path: str,
    exclude: Optional[List] = None,
    include: Optional[List] = None,
    owner: str | None = None,
) -> Container:
    """Load a python package source code to a python environment container.

    Args:
        context (PipelineContext): The current test context, providing the repository directory from which the python sources will be pulled.
        python_environment (Container): An existing python environment in which the package will be installed.
        package_source_code_path (str): The local path to the package source code.
        additional_dependency_groups (Optional[List]): extra_requires dependency of setup.py to install. Defaults to None.
        exclude (Optional[List]): A list of file or directory to exclude from the python package source code.
        include (Optional[List]): A list of file or directory to include from the python package source code.
        owner (str, optional): The owner of the mounted directory. Defaults to None.
    Returns:
        Container: A python environment container with the python package source code.
    """
    package_source_code_directory: Directory = context.get_repo_dir(package_source_code_path, exclude=exclude, include=include)
    work_dir_path = f"/{package_source_code_path}"
    container = python_environment.with_mounted_directory(work_dir_path, package_source_code_directory, owner=owner).with_workdir(
        work_dir_path
    )
    return container


async def find_local_dependencies_in_setup_py(python_package: Container) -> List[str]:
    """Find local dependencies of a python package in its setup.py file.

    Args:
        python_package (Container): A python package container.

    Returns:
        List[str]: Paths to the local dependencies relative to the airbyte repo.
    """
    setup_file_content = await get_file_contents(python_package, "setup.py")
    if not setup_file_content:
        return []

    local_setup_dependency_paths = []
    with_egg_info = python_package.with_exec(["python", "setup.py", "egg_info"], use_entrypoint=True)
    egg_info_output = await with_egg_info.stdout()
    dependency_in_requires_txt = []
    for line in egg_info_output.split("\n"):
        if line.startswith("writing requirements to"):
            # Find the path to the requirements.txt file that was generated by calling egg_info
            requires_txt_path = line.replace("writing requirements to", "").strip()
            requirements_txt_content = await with_egg_info.file(requires_txt_path).contents()
            dependency_in_requires_txt = requirements_txt_content.split("\n")

    for dependency_line in dependency_in_requires_txt:
        if "file://" in dependency_line:
            match = re.search(r"file:///(.+)", dependency_line)
            if match:
                local_setup_dependency_paths.append([match.group(1)][0])
    return local_setup_dependency_paths


async def find_local_dependencies_in_requirements_txt(python_package: Container, package_source_code_path: str) -> List[str]:
    """Find local dependencies of a python package in a requirements.txt file.

    Args:
        python_package (Container): A python environment container with the python package source code.
        package_source_code_path (str): The local path to the python package source code.

    Returns:
        List[str]: Paths to the local dependencies relative to the airbyte repo.
    """
    requirements_txt_content = await get_file_contents(python_package, "requirements.txt")
    if not requirements_txt_content:
        return []

    local_requirements_dependency_paths = []
    for line in requirements_txt_content.split("\n"):
        # Some package declare themselves as a requirement in requirements.txt,
        # #Without line != "-e ." the package will be considered a dependency of itself which can cause an infinite loop
        if line.startswith("-e .") and line != "-e .":
            local_dependency_path = str((Path(package_source_code_path) / Path(line[3:])).resolve().relative_to(Path.cwd()))
            local_requirements_dependency_paths.append(local_dependency_path)
    return local_requirements_dependency_paths


async def find_local_python_dependencies(
    context: PipelineContext,
    package_source_code_path: str,
    search_dependencies_in_setup_py: bool = True,
    search_dependencies_in_requirements_txt: bool = True,
) -> List[str]:
    """Find local python dependencies of a python package. The dependencies are found in the setup.py and requirements.txt files.

    Args:
        context (PipelineContext): The current pipeline context, providing a dagger client and a repository directory.
        package_source_code_path (str): The local path to the python package source code.
        search_dependencies_in_setup_py (bool, optional): Whether to search for local dependencies in the setup.py file. Defaults to True.
        search_dependencies_in_requirements_txt (bool, optional): Whether to search for local dependencies in the requirements.txt file. Defaults to True.
    Returns:
        List[str]: Paths to the local dependencies relative to the airbyte repo.
    """
    python_environment = with_python_base(context)
    container = with_python_package(context, python_environment, package_source_code_path)

    local_dependency_paths = []
    if search_dependencies_in_setup_py:
        local_dependency_paths += await find_local_dependencies_in_setup_py(container)
    if search_dependencies_in_requirements_txt:
        local_dependency_paths += await find_local_dependencies_in_requirements_txt(container, package_source_code_path)

    transitive_dependency_paths = []
    for local_dependency_path in local_dependency_paths:
        # Transitive local dependencies installation is achieved by calling their setup.py file, not their requirements.txt file.
        transitive_dependency_paths += await find_local_python_dependencies(context, local_dependency_path, True, False)

    all_dependency_paths = local_dependency_paths + transitive_dependency_paths
    if all_dependency_paths:
        context.logger.debug(f"Found local dependencies for {package_source_code_path}: {all_dependency_paths}")
    return all_dependency_paths


def _install_python_dependencies_from_setup_py(
    container: Container,
    additional_dependency_groups: Optional[Sequence[str]] = None,
) -> Container:
    install_connector_package_cmd = ["pip", "install", "."]
    container = container.with_exec(install_connector_package_cmd)

    if additional_dependency_groups:
        # e.g. .[dev,tests]
        group_string = f".[{','.join(additional_dependency_groups)}]"
        group_install_cmd = ["pip", "install", group_string]

        container = container.with_exec(group_install_cmd)

    return container


def _install_python_dependencies_from_requirements_txt(container: Container) -> Container:
    install_requirements_cmd = ["pip", "install", "-r", "requirements.txt"]
    return container.with_exec(install_requirements_cmd)


def _install_python_dependencies_from_poetry(
    container: Container,
    additional_dependency_groups: Optional[Sequence[str]] = None,
    install_root_package: bool = True,
) -> Container:
    pip_install_poetry_cmd = ["pip", "install", "poetry"]
    poetry_disable_virtual_env_cmd = ["poetry", "config", "virtualenvs.create", "false"]
    poetry_install_cmd = ["poetry", "install"]
    poetry_check_cmd = ["poetry", "check"]
    if not install_root_package:
        poetry_install_cmd += ["--no-root"]
    if additional_dependency_groups:
        for group in additional_dependency_groups:
            poetry_install_cmd += ["--with", group]
    else:
        poetry_install_cmd += ["--only", "main"]
    return (
        container.with_exec(pip_install_poetry_cmd)
        .with_exec(poetry_disable_virtual_env_cmd)
        .with_exec(poetry_check_cmd)
        .with_exec(poetry_install_cmd)
    )


async def with_installed_python_package(
    context: PipelineContext,
    python_environment: Container,
    package_source_code_path: str,
    user: str,
    additional_dependency_groups: Optional[Sequence[str]] = None,
    exclude: Optional[List] = None,
    include: Optional[List] = None,
    install_root_package: bool = True,
) -> Container:
    """Install a python package in a python environment container.

    Args:
        context (PipelineContext): The current test context, providing the repository directory from which the python sources will be pulled.
        python_environment (Container): An existing python environment in which the package will be installed.
        package_source_code_path (str): The local path to the package source code.
        user (str): The user to use in the container.
        additional_dependency_groups (Optional[Sequence[str]]): extra_requires dependency of setup.py to install. Defaults to None.
        exclude (Optional[List]): A list of file or directory to exclude from the python package source code.
        include (Optional[List]): A list of file or directory to include from the python package source code.
        install_root_package (bool): Whether to install the root package. Defaults to True.

    Returns:
        Container: A python environment container with the python package installed.
    """

    container = with_python_package(context, python_environment, package_source_code_path, exclude=exclude, include=include, owner=user)
    local_dependencies = await find_local_python_dependencies(context, package_source_code_path)

    for dependency_directory in local_dependencies:
        container = container.with_mounted_directory("/" + dependency_directory, context.get_repo_dir(dependency_directory), owner=user)

    has_setup_py = await check_path_in_workdir(container, "setup.py")
    has_requirements_txt = await check_path_in_workdir(container, "requirements.txt")
    has_pyproject_toml = await check_path_in_workdir(container, "pyproject.toml")

    container = container.with_user("root")
    # All of these will require root access to install dependencies
    # Dependencies are installed at the system level, if the user is not root it is not allowed to install system level dependencies
    if has_pyproject_toml:
        container = with_poetry_cache(container, context.dagger_client, owner=user)
        container = _install_python_dependencies_from_poetry(container, additional_dependency_groups, install_root_package)
    elif has_setup_py:
        container = with_pip_cache(container, context.dagger_client)
        container = _install_python_dependencies_from_setup_py(container, additional_dependency_groups)
    elif has_requirements_txt:
        container = with_pip_cache(container, context.dagger_client)
        container = _install_python_dependencies_from_requirements_txt(container)

    container = container.with_user(user)

    return container


def apply_python_development_overrides(context: ConnectorContext, connector_container: Container, current_user: str) -> Container:
    # Run the connector using the local cdk if flag is set
    if context.use_local_cdk:
        # Assume CDK is cloned in a sibling dir to `airbyte`:
        path_to_cdk = str(Path(PATH_TO_LOCAL_CDK).resolve())
        if not Path(path_to_cdk).exists():
            raise UsageError(
                f"Local CDK not found at '{path_to_cdk}'. Please clone the CDK repository in a sibling directory to the airbyte repository. Or use --use-cdk-ref to specify a CDK ref."
            )
        context.logger.info(f"Using local CDK found at: '{path_to_cdk}'")
        directory_to_mount = context.dagger_client.host().directory(path_to_cdk)
        cdk_mount_dir = "/airbyte-cdk/python"

        context.logger.info(f"Mounting CDK from '{path_to_cdk}' to '{cdk_mount_dir}'")
        # Install the airbyte-cdk package from the local directory
        connector_container = (
            connector_container.with_env_variable(
                "POETRY_DYNAMIC_VERSIONING_BYPASS",
                "0.0.0-dev.0",  # Replace dynamic versioning with dev version
            )
            .with_directory(
                cdk_mount_dir,
                directory_to_mount,
                owner=current_user,
            )
            # We switch to root as otherwise we get permission errors when installing the package
            # Permissions errors are caused by the fact that the airbyte user does not have a home directory
            # Pip tries to write to /nonexistent which does not exist and on which the airbyte user does not have permissions
            # We could create a proper home directory for the airbyte user, but that should be done at the base image level.
            # Installing as root should not cause any issues as the container is ephemeral and the image is not pushed to a registry.
            # Moreover this install is a system-wide install so the airbyte user will be able to use the package.
            .with_user("root")
            .with_exec(
                ["pip", "install", "--no-cache-dir", "--force-reinstall", f"{cdk_mount_dir}"],
                # TODO: Consider moving to Poetry-native installation:
                # ["poetry", "add", cdk_mount_dir]
            )
            # Switch back to the original user
            .with_user(current_user)
        )
    elif context.use_cdk_ref:
        cdk_ref = context.use_cdk_ref
        if " " in cdk_ref:
            raise ValueError("CDK ref should not contain spaces")

        context.logger.info("Using CDK ref: '{cdk_ref}'")
        # Install the airbyte-cdk package from provided ref
        connector_container = (
            connector_container.with_user("root")
            .with_exec(
                [
                    "apt-get",
                    "install",
                    "-y",
                    "git",
                ]
            )
            .with_exec(
                [
                    "pip",
                    "install",
                    f"git+https://github.com/airbytehq/airbyte-python-cdk.git#{cdk_ref}",
                ],
            )
            .with_user(current_user)
        )
    return connector_container


async def with_python_connector_installed(
    context: ConnectorContext,
    python_container: Container,
    connector_source_path: str,
    user: str,
    additional_dependency_groups: Optional[Sequence[str]] = None,
    exclude: Optional[List[str]] = None,
    include: Optional[List[str]] = None,
    install_root_package: bool = True,
) -> Container:
    """Install an airbyte python connectors  dependencies."""

    # Download the latest CDK version to update the pip cache.
    # This is a hack to ensure we always get the latest CDK version installed in connectors not pinning the CDK version.
    await hacks.cache_latest_cdk(context)
    container = await with_installed_python_package(
        context,
        python_container,
        connector_source_path,
        user,
        additional_dependency_groups=additional_dependency_groups,
        exclude=exclude,
        include=include,
        install_root_package=install_root_package,
    )

    container = await apply_python_development_overrides(context, container, user)
    await raise_if_not_user(container, user)
    return container


def with_pip_packages(base_container: Container, packages_to_install: List[str]) -> Container:
    """Installs packages using pip
    Args:
        context (Container): A container with python installed

    Returns:
        Container: A container with the pip packages installed.

    """
    package_install_command = ["pip", "install"]
    return base_container.with_exec(package_install_command + packages_to_install)
