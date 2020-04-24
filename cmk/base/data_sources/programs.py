#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import abc
import os
import sys
from logging import Logger  # pylint: disable=unused-import
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple, Union  # pylint: disable=unused-import

import six

if sys.version_info[0] >= 3:
    from pathlib import Path  # pylint: disable=import-error
else:
    from pathlib2 import Path  # pylint: disable=import-error

from cmk.fetchers import ProgramDataFetcher  # pylint: disable=cmk-module-layer-violation

import cmk.utils.paths

import cmk.base.config as config
import cmk.base.core_config as core_config
from cmk.base.exceptions import MKAgentError
from cmk.base.check_utils import CheckPluginName  # pylint: disable=unused-import
from cmk.utils.type_defs import (  # pylint: disable=unused-import
    HostName, HostAddress)

from .abstract import CheckMKAgentDataSource, RawAgentData  # pylint: disable=unused-import

#.
#   .--Datasoure Programs--------------------------------------------------.
#   |      ____        _                     ____                          |
#   |     |  _ \  __ _| |_ __ _ ___ _ __ ___|  _ \ _ __ ___   __ _         |
#   |     | | | |/ _` | __/ _` / __| '__/ __| |_) | '__/ _ \ / _` |        |
#   |     | |_| | (_| | || (_| \__ \ | | (__|  __/| | | (_) | (_| |_       |
#   |     |____/ \__,_|\__\__,_|___/_|  \___|_|   |_|  \___/ \__, (_)      |
#   |                                                        |___/         |
#   +----------------------------------------------------------------------+
#   | Fetching agent data from program calls instead of an agent           |
#   '----------------------------------------------------------------------'


class ProgramDataSource(CheckMKAgentDataSource):
    """Abstract base class for all data source classes that execute external programs"""
    @property
    @abc.abstractmethod
    def source_cmdline(self):
        # type: () -> str
        """Return the command line to the source."""
        raise NotImplementedError()

    @property
    @abc.abstractmethod
    def source_stdin(self):
        # type: () -> Optional[str]
        """Return the standard in of the source, or None."""
        raise NotImplementedError()

    def _cpu_tracking_id(self):
        # type: () -> str
        return "ds"

    def _execute(self):
        # type: () -> RawAgentData
        self._logger.debug("Calling external program %r" % (self.source_cmdline))
        with ProgramDataFetcher(self.source_cmdline, self.source_stdin, self._logger) as fetcher:
            return fetcher.data()
        raise MKAgentError("Failed to read data")

    def describe(self):
        # type: () -> str
        """Return a short textual description of the agent"""
        response = ["Program: %s" % self.source_cmdline]
        if self.source_stdin:
            response.extend(["  Program stdin:", self.source_stdin])
        return "\n".join(response)


class DSProgramDataSource(ProgramDataSource):
    def __init__(self, hostname, ipaddress, command_template):
        # type: (HostName, Optional[HostAddress], str) -> None
        super(DSProgramDataSource, self).__init__(hostname, ipaddress)
        self._command_template = command_template

    def id(self):
        # type: () -> str
        return "agent"

    def name(self):
        # type: () -> str
        """Return a unique (per host) textual identification of the data source"""
        program = self.source_cmdline.split(" ")[0]
        return os.path.basename(program)

    @property
    def source_cmdline(self):
        # type: () -> str
        cmd = self._command_template
        cmd = self._translate_legacy_macros(cmd)
        cmd = self._translate_host_macros(cmd)
        return cmd

    @property
    def source_stdin(self):
        return None

    def _translate_legacy_macros(self, cmd):
        # type: (str) -> str
        # Make "legacy" translation. The users should use the $...$ macros in future
        return cmd.replace("<IP>", self._ipaddress or "").replace("<HOST>", self._hostname)

    def _translate_host_macros(self, cmd):
        # type: (str) -> str
        attrs = core_config.get_host_attributes(self._hostname, self._config_cache)
        if self._host_config.is_cluster:
            parents_list = core_config.get_cluster_nodes_for_config(self._config_cache,
                                                                    self._host_config)
            attrs.setdefault("alias", "cluster of %s" % ", ".join(parents_list))
            attrs.update(
                core_config.get_cluster_attributes(self._config_cache, self._host_config,
                                                   parents_list))

        macros = core_config.get_host_macros_from_attributes(self._hostname, attrs)
        return six.ensure_str(core_config.replace_macros(cmd, macros))


SpecialAgentConfiguration = NamedTuple(
    "SpecialAgentConfiguration",
    [
        ("args", Union[str, List[str]]),
        ("stdin", Optional[str]),  # TODO: Why do we need to distinguish between "" and None???
    ])


class SpecialAgentDataSource(ProgramDataSource):
    def __init__(self, hostname, ipaddress, special_agent_id, params):
        # type: (HostName, Optional[HostAddress], str, Dict) -> None
        self._special_agent_id = special_agent_id
        super(SpecialAgentDataSource, self).__init__(hostname, ipaddress)
        self._params = params

    def id(self):
        # type: () -> str
        return "special_%s" % self._special_agent_id

    @property
    def special_agent_plugin_file_name(self):
        # type: () -> str
        return "agent_%s" % self._special_agent_id

    # TODO: Can't we make this more specific in case of special agents?
    def _gather_check_plugin_names(self):
        # type: () -> Set[CheckPluginName]
        return config.discoverable_tcp_checks()

    @property
    def _source_path(self):
        # type: () -> Path
        local_path = cmk.utils.paths.local_agents_dir / "special" / self.special_agent_plugin_file_name
        if local_path.exists():
            return local_path
        return Path(cmk.utils.paths.agents_dir) / "special" / self.special_agent_plugin_file_name

    @property
    def _source_args(self):
        # type: () -> str
        info_func = config.special_agent_info[self._special_agent_id]
        agent_configuration = info_func(self._params, self._hostname, self._ipaddress)
        if isinstance(agent_configuration, SpecialAgentConfiguration):
            cmd_arguments = agent_configuration.args  # type: Union[str, List[Union[int, float, str, Tuple[Any, Any, Any]]]]
        elif isinstance(agent_configuration, str):
            cmd_arguments = agent_configuration
        elif isinstance(agent_configuration, list):
            cmd_arguments = [arg for arg in agent_configuration if isinstance(arg, str)]
        else:
            raise Exception("invalid agent configuration %r" % (agent_configuration,))

        return config.prepare_check_command(cmd_arguments, self._hostname, description=None)

    @property
    def source_cmdline(self):
        # type: () -> str
        """Create command line using the special_agent_info"""
        return "%s %s" % (self._source_path, self._source_args)

    @property
    def source_stdin(self):
        # type: () -> Optional[str]
        """Create command line using the special_agent_info"""
        info_func = config.special_agent_info[self._special_agent_id]
        agent_configuration = info_func(self._params, self._hostname, self._ipaddress)
        if isinstance(agent_configuration, SpecialAgentConfiguration):
            return agent_configuration.stdin
        return None
