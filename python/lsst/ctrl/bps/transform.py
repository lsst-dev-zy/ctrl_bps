# This file is part of ctrl_bps.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This software is dual licensed under the GNU General Public License and also
# under a 3-clause BSD license. Recipients may choose which of these licenses
# to use; please see the files gpl-3.0.txt and/or bsd_license.txt,
# respectively.  If you choose the GPL option then the following text applies
# (but note that there is still no warranty even if you opt for BSD instead):
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Driver for the transformation of a QuantumGraph into a generic workflow.
"""

import copy
import dataclasses
import logging
import math
import os
import re

from lsst.utils.logging import VERBOSE
from lsst.utils.timer import time_this, timeMethod

from . import (
    DEFAULT_MEM_RETRIES,
    BpsConfig,
    GenericWorkflow,
    GenericWorkflowExec,
    GenericWorkflowFile,
    GenericWorkflowJob,
)
from .bps_utils import (
    WhenToSaveQuantumGraphs,
    _create_execution_butler,
    create_job_quantum_graph_filename,
    save_qg_subgraph,
)

# All available job attributes.
_ATTRS_ALL = frozenset([field.name for field in dataclasses.fields(GenericWorkflowJob)])

# Job attributes that need to be set to their maximal value in the cluster.
_ATTRS_MAX = frozenset(
    {
        "memory_multiplier",
        "number_of_retries",
        "request_cpus",
        "request_memory",
        "request_memory_max",
    }
)

# Job attributes that need to be set to sum of their values in the cluster.
_ATTRS_SUM = frozenset(
    {
        "request_disk",
        "request_walltime",
    }
)

# Job attributes do not fall into a specific category
_ATTRS_MISC = frozenset(
    {
        "label",  # taskDef labels aren't same in job and may not match job label
        "cmdvals",
        "profile",
        "attrs",
    }
)

# Attributes that need to be the same for each quanta in the cluster.
_ATTRS_UNIVERSAL = frozenset(_ATTRS_ALL - (_ATTRS_MAX | _ATTRS_MISC | _ATTRS_SUM))

_LOG = logging.getLogger(__name__)


@timeMethod(logger=_LOG, logLevel=VERBOSE)
def transform(config, prefix):
    _, when_create = config.search(".executionButler.whenCreate")
    if when_create.upper() == "TRANSFORM":
        _, execution_butler_dir = config.search(".bps_defined.executionButlerDir")
        _LOG.info("Creating execution butler in '%s'", execution_butler_dir)
        with time_this(log=_LOG, level=logging.INFO, prefix=None, msg="Creating execution butler completed"):
            _create_execution_butler(config, config["runQgraphFile"], execution_butler_dir, prefix)

    _, name = config.search("uniqProcName", opt={"required": True})

    generic_workflow = create_generic_workflow(config, name, prefix)
    generic_workflow_config = create_generic_workflow_config(config, prefix)

    return generic_workflow, generic_workflow_config

def add_workflow_init_nodes(config, qgraph, generic_workflow):
    """Add nodes to workflow graph that perform initialization steps.

    Assumes that all of the initialization should be executed prior to any
    of the current workflow.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        BPS configuration.
    qgraph : `lsst.pipe.base.graph.QuantumGraph`
        The quantum graph the generic workflow represents.
    generic_workflow : `lsst.ctrl.bps.GenericWorkflow`
        Generic workflow to which the initialization steps should be added.
    """
    # Create a workflow graph that will have task and file nodes necessary for
    # initializing the pipeline execution
    init_workflow = create_init_workflow(config, qgraph, generic_workflow.get_file("runQgraphFile"))
    _LOG.debug("init_workflow nodes = %s", init_workflow.nodes())
    generic_workflow.add_workflow_source(init_workflow)


def create_init_workflow(config, qgraph, qgraph_gwfile):
    """Create workflow for running initialization job(s).

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        BPS configuration.
    qgraph : `lsst.pipe.base.graph.QuantumGraph`
        The quantum graph the generic workflow represents.
    qgraph_gwfile : `lsst.ctrl.bps.GenericWorkflowFile`
        File object for the full run QuantumGraph file.

    Returns
    -------
    init_workflow : `lsst.ctrl.bps.GenericWorkflow`
        GenericWorkflow consisting of job(s) to initialize workflow.
    """
    _LOG.debug("creating init subgraph")
    _LOG.debug("creating init task input(s)")
    search_opt = {
        "curvals": {"curr_pipetask": "pipetaskInit"},
        "replaceVars": False,
        "expandEnvVars": False,
        "replaceEnvVars": True,
        "required": False,
    }
    found, value = config.search("computeSite", opt=search_opt)
    if found:
        search_opt["curvals"]["curr_site"] = value
    found, value = config.search("computeCloud", opt=search_opt)
    if found:
        search_opt["curvals"]["curr_cloud"] = value

    init_workflow = GenericWorkflow("init")
    init_workflow.add_file(qgraph_gwfile)

    # create job for executing --init-only
    gwjob = GenericWorkflowJob("pipetaskInit", label="pipetaskInit")

    job_values = _get_job_values(config, search_opt, "runQuantumCommand")
    job_values["name"] = "pipetaskInit"
    job_values["label"] = "pipetaskInit"

    # Adjust job attributes values if necessary.
    _handle_job_values(job_values, gwjob)

    # Pick a node id for each task (not quantum!) to avoid reading the entire
    # quantum graph during the initialization stage.
    node_ids = []
    for task_label in qgraph.pipeline_graph.tasks:
        task_def = qgraph.findTaskDefByLabel(task_label)
        node = next(iter(qgraph.getNodesForTask(task_def)))
        node_ids.append(node.nodeId)
    gwjob.cmdvals["qgraphId"] = qgraph.graphID
    gwjob.cmdvals["qgraphNodeId"] = ",".join(sorted([f"{node_id}" for node_id in node_ids]))

    init_workflow.add_job(gwjob)

    # Lookup butler values
    _, when_create = config.search(".executionButler.whenCreate", opt=search_opt)
    _, butler_config = config.search("butlerConfig", opt=search_opt)
    _, execution_butler_dir = config.search(".bps_defined.executionButlerDir", opt=search_opt)
    prefix = config["submitPath"]
    butler_gwfile = _get_butler_gwfile(prefix, when_create, butler_config, execution_butler_dir)

    init_workflow.add_job_inputs(gwjob.name, [qgraph_gwfile, butler_gwfile])
    _enhance_command(config, init_workflow, gwjob, {})

    return init_workflow


def _enhance_command(config, generic_workflow, gwjob, cached_job_values):
    """Enhance command line with env and file placeholders
    and gather command line values.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        BPS configuration.
    generic_workflow : `lsst.ctrl.bps.GenericWorkflow`
        Generic workflow that contains the job.
    gwjob : `lsst.ctrl.bps.GenericWorkflowJob`
        Generic workflow job to which the updated executable, arguments,
        and values should be saved.
    cached_job_values : `dict` [`str`, dict[`str`, `Any`]]
        Cached values common across jobs with same label.  Updated if values
        aren't already saved for given gwjob's label.
    """
    _LOG.debug("gwjob given to _enhance_command: %s", gwjob)

    search_opt = {
        "curvals": {"curr_pipetask": gwjob.label},
        "replaceVars": False,
        "expandEnvVars": False,
        "replaceEnvVars": True,
        "required": False,
    }

    if gwjob.label not in cached_job_values:
        cached_job_values[gwjob.label] = {}
        # Allowing whenSaveJobQgraph and useLazyCommands per pipetask label.
        key = "whenSaveJobQgraph"
        _, when_save = config.search(key, opt=search_opt)
        cached_job_values[gwjob.label][key] = WhenToSaveQuantumGraphs[when_save.upper()]

        key = "useLazyCommands"
        search_opt["default"] = True
        _, cached_job_values[gwjob.label][key] = config.search(key, opt=search_opt)
        del search_opt["default"]

    # Change qgraph variable to match whether using run or per-job qgraph
    # Note: these are lookup keys, not actual physical filenames.
    if cached_job_values[gwjob.label]["whenSaveJobQgraph"] == WhenToSaveQuantumGraphs.NEVER:
        gwjob.arguments = gwjob.arguments.replace("{qgraphFile}", "{runQgraphFile}")
    elif gwjob.name == "pipetaskInit":
        gwjob.arguments = gwjob.arguments.replace("{qgraphFile}", "{runQgraphFile}")
    else:  # Needed unique file keys for per-job QuantumGraphs
        gwjob.arguments = gwjob.arguments.replace("{qgraphFile}", f"{{qgraphFile_{gwjob.name}}}")

    # Replace files with special placeholders
    for gwfile in generic_workflow.get_job_inputs(gwjob.name):
        gwjob.arguments = gwjob.arguments.replace(f"{{{gwfile.name}}}", f"<FILE:{gwfile.name}>")
    for gwfile in generic_workflow.get_job_outputs(gwjob.name):
        gwjob.arguments = gwjob.arguments.replace(f"{{{gwfile.name}}}", f"<FILE:{gwfile.name}>")

    # Save dict of other values needed to complete command line.
    # (Be careful to not replace env variables as they may
    # be different in compute job.)
    search_opt["replaceVars"] = True

    for key in re.findall(r"{([^}]+)}", gwjob.arguments):
        if key not in gwjob.cmdvals:
            if key not in cached_job_values[gwjob.label]:
                _, cached_job_values[gwjob.label][key] = config.search(key, opt=search_opt)
            gwjob.cmdvals[key] = cached_job_values[gwjob.label][key]

    # backwards compatibility
    if not cached_job_values[gwjob.label]["useLazyCommands"]:
        if "bpsUseShared" not in cached_job_values[gwjob.label]:
            key = "bpsUseShared"
            search_opt["default"] = True
            _, cached_job_values[gwjob.label][key] = config.search(key, opt=search_opt)
            del search_opt["default"]

        gwjob.arguments = _fill_arguments(
            cached_job_values[gwjob.label]["bpsUseShared"], generic_workflow, gwjob.arguments, gwjob.cmdvals
        )


def _fill_arguments(use_shared, generic_workflow, arguments, cmdvals):
    """Replace placeholders in command line string in job.

    Parameters
    ----------
    use_shared : `bool`
        Whether using shared filesystem.
    generic_workflow : `lsst.ctrl.bps.GenericWorkflow`
        Generic workflow containing the job.
    arguments : `str`
        String containing placeholders.
    cmdvals : `dict` [`str`, `Any`]
        Any command line values that can be used to replace placeholders.

    Returns
    -------
    arguments : `str`
        Command line with FILE and ENV placeholders replaced.
    """
    # Replace file placeholders
    for file_key in re.findall(r"<FILE:([^>]+)>", arguments):
        gwfile = generic_workflow.get_file(file_key)
        if not gwfile.wms_transfer:
            # Must assume full URI if in command line and told WMS is not
            # responsible for transferring file.
            uri = gwfile.src_uri
        elif use_shared:
            if gwfile.job_shared:
                # Have shared filesystems and jobs can share file.
                uri = gwfile.src_uri
            else:
                # Taking advantage of inside knowledge.  Not future-proof.
                # Temporary fix until have job wrapper that pulls files
                # within job.
                if gwfile.name == "butlerConfig" and os.path.splitext(gwfile.src_uri)[1] != ".yaml":
                    uri = "butler.yaml"
                else:
                    uri = os.path.basename(gwfile.src_uri)
        else:  # Using push transfer
            uri = os.path.basename(gwfile.src_uri)

        arguments = arguments.replace(f"<FILE:{file_key}>", uri)

    # Replace env placeholder with submit-side values
    arguments = re.sub(r"<ENV:([^>]+)>", r"$\1", arguments)
    arguments = os.path.expandvars(arguments)

    # Replace remaining vars
    arguments = arguments.format(**cmdvals)

    return arguments


def _get_butler_gwfile(prefix, when_create, butler_config, execution_butler_dir):
    """Get butler location to be used by job.

    Parameters
    ----------
    prefix : `str`
        Root path for any output files.
    when_create : `str`
        When to create the execution butler used to determine whether job is
        using execution butler or not.
    butler_config : `str`
        Location of central butler repositories config file.
    execution_butler_dir : `str`
        Location of execution butler repository.

    Returns
    -------
    gwfile : `lsst.ctrl.bps.GenericWorkflowFile`
        Representation of butler location.
    """
    if when_create.upper() == "NEVER":
        wms_transfer = False
        job_access_remote = True
        job_shared = True
    else:
        butler_config = execution_butler_dir
        if not butler_config.startswith("/"):
            butler_config = f"{prefix}/{butler_config}"
        wms_transfer = True
        job_access_remote = False
        job_shared = False

    gwfile = GenericWorkflowFile(
        "butlerConfig",
        src_uri=butler_config,
        wms_transfer=wms_transfer,
        job_access_remote=job_access_remote,
        job_shared=job_shared,
    )

    return gwfile


def _get_qgraph_gwfile(config, save_qgraph_per_job, gwjob, run_qgraph_file, prefix):
    """Get qgraph location to be used by job.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        Bps configuration.
    save_qgraph_per_job : `lsst.ctrl.bps.bps_utils.WhenToSaveQuantumGraphs`
        What submission stage to save per-job qgraph files (or NEVER)
    gwjob : `lsst.ctrl.bps.GenericWorkflowJob`
        Job for which determining QuantumGraph file.
    run_qgraph_file : `lsst.ctrl.bps.GenericWorkflowFile`
        File representation of the full run QuantumGraph.
    prefix : `str`
        Path prefix for any files written.

    Returns
    -------
    gwfile : `lsst.ctrl.bps.GenericWorkflowFile`
        Representation of butler location (may not include filename).
    """
    qgraph_gwfile = None
    if save_qgraph_per_job != WhenToSaveQuantumGraphs.NEVER:
        qgraph_gwfile = GenericWorkflowFile(
            f"qgraphFile_{gwjob.name}",
            src_uri=create_job_quantum_graph_filename(config, gwjob, prefix),
            wms_transfer=True,
            job_access_remote=True,
            job_shared=True,
        )
    else:
        qgraph_gwfile = run_qgraph_file

    return qgraph_gwfile


def _get_job_values(config, search_opt, cmd_line_key):
    """Gather generic workflow job values from the bps config.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        Bps configuration.
    search_opt : `dict` [`str`, `Any`]
        Search options to be used when searching config.
    cmd_line_key : `str` or None
        Which command line key to search for (e.g., "runQuantumCommand").

    Returns
    -------
    job_values : `dict` [ `str`, `Any` ]`
        A mapping between job attributes and their values.
    """
    _LOG.debug("cmd_line_key=%s, search_opt=%s", cmd_line_key, search_opt)

    # Create a dummy job to easily access the default values.
    default_gwjob = GenericWorkflowJob("default_job")

    job_values = {}
    for attr in _ATTRS_ALL:
        # Variable names in yaml are camel case instead of snake case.
        yaml_name = re.sub(r"_(\S)", lambda match: match.group(1).upper(), attr)
        found, value = config.search(yaml_name, opt=search_opt)
        if found:
            job_values[attr] = value
        else:
            job_values[attr] = getattr(default_gwjob, attr)

    # If the automatic memory scaling is enabled (i.e. the memory multiplier
    # is set and it is a positive number greater than 1.0), adjust number
    # of retries when necessary.  If the memory multiplier is invalid, disable
    # automatic memory scaling.
    if job_values["memory_multiplier"] is not None:
        if math.ceil(float(job_values["memory_multiplier"])) > 1:
            if job_values["number_of_retries"] is None:
                job_values["number_of_retries"] = DEFAULT_MEM_RETRIES
        else:
            job_values["memory_multiplier"] = None

    if cmd_line_key:
        found, cmdline = config.search(cmd_line_key, opt=search_opt)
        # Make sure cmdline isn't None as that could be sent in as a
        # default value in search_opt.
        if found and cmdline:
            cmd, args = cmdline.split(" ", 1)
            job_values["executable"] = GenericWorkflowExec(os.path.basename(cmd), cmd, False)
            if args:
                job_values["arguments"] = args

    return job_values


def _handle_job_values(quantum_job_values, gwjob, attributes=_ATTRS_ALL):
    """Set the job attributes in the cluster to their correct values.

    Parameters
    ----------
    quantum_job_values : `dict` [`str`, Any]
        Job values for running single Quantum.
    gwjob : `lsst.ctrl.bps.GenericWorkflowJob`
        Generic workflow job in which to store the universal values.
    attributes : `Iterable` [`str`], optional
        Job attributes to be set in the job following different rules.
        The default value is _ATTRS_ALL.
    """
    _LOG.debug("Call to _handle_job_values")
    _handle_job_values_universal(quantum_job_values, gwjob, attributes)
    _handle_job_values_max(quantum_job_values, gwjob, attributes)
    _handle_job_values_sum(quantum_job_values, gwjob, attributes)


def _handle_job_values_universal(quantum_job_values, gwjob, attributes=_ATTRS_UNIVERSAL):
    """Handle job attributes that must have the same value for every quantum
    in the cluster.

    Parameters
    ----------
    quantum_job_values : `dict` [`str`, Any]
        Job values for running single Quantum.
    gwjob : `lsst.ctrl.bps.GenericWorkflowJob`
        Generic workflow job in which to store the universal values.
    attributes : `Iterable` [`str`], optional
        Job attributes to be set in the job following different rules.
        The default value is _ATTRS_UNIVERSAL.
    """
    for attr in _ATTRS_UNIVERSAL & set(attributes):
        _LOG.debug(
            "Handling job %s (job=%s, quantum=%s)",
            attr,
            getattr(gwjob, attr),
            quantum_job_values.get(attr, "MISSING"),
        )
        current_value = getattr(gwjob, attr)
        try:
            quantum_value = quantum_job_values[attr]
        except KeyError:
            continue
        else:
            if not current_value:
                setattr(gwjob, attr, quantum_value)
            elif current_value != quantum_value:
                _LOG.error(
                    "Inconsistent value for %s in Cluster %s Quantum Number %s\n"
                    "Current cluster value: %s\n"
                    "Quantum value: %s",
                    attr,
                    gwjob.name,
                    quantum_job_values.get("qgraphNodeId", "MISSING"),
                    current_value,
                    quantum_value,
                )
                raise RuntimeError(f"Inconsistent value for {attr} in cluster {gwjob.name}.")


def _handle_job_values_max(quantum_job_values, gwjob, attributes=_ATTRS_MAX):
    """Handle job attributes that should be set to their maximum value in
    the in cluster.

    Parameters
    ----------
    quantum_job_values : `dict` [`str`, `Any`]
        Job values for running single Quantum.
    gwjob : `lsst.ctrl.bps.GenericWorkflowJob`
        Generic workflow job in which to store the aggregate values.
    attributes : `Iterable` [`str`], optional
        Job attributes to be set in the job following different rules.
        The default value is _ATTR_MAX.
    """
    for attr in _ATTRS_MAX & set(attributes):
        current_value = getattr(gwjob, attr)
        try:
            quantum_value = quantum_job_values[attr]
        except KeyError:
            continue
        else:
            needs_update = False
            if current_value is None:
                if quantum_value is not None:
                    needs_update = True
            else:
                if quantum_value is not None and current_value < quantum_value:
                    needs_update = True
            if needs_update:
                setattr(gwjob, attr, quantum_value)

                # When updating memory requirements for a job, check if memory
                # autoscaling is enabled. If it is, always use the memory
                # multiplier and the number of retries which comes with the
                # quantum.
                #
                # Note that as a result, the quantum with the biggest memory
                # requirements will determine whether the memory autoscaling
                # will be enabled (or disabled) depending on the value of its
                # memory multiplier.
                if attr == "request_memory":
                    gwjob.memory_multiplier = quantum_job_values["memory_multiplier"]
                    if gwjob.memory_multiplier is not None:
                        gwjob.number_of_retries = quantum_job_values["number_of_retries"]


def _handle_job_values_sum(quantum_job_values, gwjob, attributes=_ATTRS_SUM):
    """Handle job attributes that are the sum of their values in the cluster.

    Parameters
    ----------
    quantum_job_values : `dict` [`str`, `Any`]
        Job values for running single Quantum.
    gwjob : `lsst.ctrl.bps.GenericWorkflowJob`
        Generic workflow job in which to store the aggregate values.
    attributes : `Iterable` [`str`], optional
        Job attributes to be set in the job following different rules.
        The default value is _ATTRS_SUM.
    """
    for attr in _ATTRS_SUM & set(attributes):
        current_value = getattr(gwjob, attr)
        if not current_value:
            setattr(gwjob, attr, quantum_job_values[attr])
        else:
            setattr(gwjob, attr, current_value + quantum_job_values[attr])

def create_generic_workflow(config, name, prefix):
    # Determine whether saving per-job QuantumGraph files in the loop.
    _, when_save = config.search("whenSaveJobQgraph", {"default": WhenToSaveQuantumGraphs.TRANSFORM.name})
    save_qgraph_per_job = WhenToSaveQuantumGraphs[when_save.upper()]

    search_opt = {"replaceVars": False, "expandEnvVars": False, "replaceEnvVars": True, "required": False}

    # Lookup butler values once
    _, when_create = config.search(".executionButler.whenCreate", opt=search_opt)
    _, butler_config = config.search("butlerConfig", opt=search_opt)
    _, execution_butler_dir = config.search(".bps_defined.executionButlerDir", opt=search_opt)

    generic_workflow = GenericWorkflow(name)
    #'''zy
    # Save full run QuantumGraph for use by jobs
    generic_workflow.add_file(
        GenericWorkflowFile(
            "runQgraphFile",
            src_uri=config["runQgraphFile"],
            wms_transfer=False,
            job_access_remote=True,
            job_shared=True,
        )
    )
    #'''

    # Cache pipetask specific or more generic job values to minimize number
    # on config searches.
    cached_job_values = {}
    cached_pipetask_values = {}

    # Add final job
    add_final_job(config, generic_workflow, prefix)

    return generic_workflow


def create_generic_workflow_config(config, prefix):
    """Create generic workflow configuration.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        Bps configuration.
    prefix : `str`
        Root path for any output files.

    Returns
    -------
    generic_workflow_config : `lsst.ctrl.bps.BpsConfig`
        Configuration accompanying the GenericWorkflow.
    """
    generic_workflow_config = BpsConfig(config)
    generic_workflow_config["workflowName"] = config["uniqProcName"]
    generic_workflow_config["workflowPath"] = prefix
    return generic_workflow_config


def add_final_job(config, generic_workflow, prefix):
    """Add final workflow job depending upon configuration.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        Bps configuration.
    generic_workflow : `lsst.ctrl.bps.GenericWorkflow`
        Generic workflow to which attributes should be added.
    prefix : `str`
        Directory in which to output final script.

    Notes
    -----
    This dispatch function was introduced to preserve the existing code
    responsible for dealing with the execution Butler (EB). Once there is
    no need to support the EB any longer it can be replaced by the function
    responsible for handling the final job.
    """
    # The order of the entries determines the priorities regarding what
    # method will be used when adding the final job if the configuration
    # provides conflicting specifications.
    dispatcher = {
        ".finalJob.whenRun": _add_final_job,
        ".executionButler.whenCreate": _add_merge_job,
    }
    for name, func in dispatcher.items():
        if name in config and config[name] != "NEVER":
            break
    else:
        raise RuntimeError("Final job specification not found")
    func(config, generic_workflow, prefix)


def _add_final_job(config, generic_workflow, prefix):
    """Add the final job.

    Depending on configuration, the final job will be added as a special job
    which will always run regardless of the exit status of the workflow or
    a regular sink node which will only run if the workflow execution finished
    with no errors.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        Bps configuration.
    generic_workflow : `lsst.ctrl.bps.GenericWorkflow`
        Generic workflow to which attributes should be added.
    prefix : `str`
        Directory in which to output final script.
    """
    _, when_run = config.search(".finalJob.whenRun")
    if when_run.upper() != "NEVER":        
        create_final_job = _make_final_job_creator("finalJob", _create_final_command)

        gwjob = create_final_job(config, generic_workflow, prefix)
        if when_run.upper() == "ALWAYS":
            generic_workflow.add_final(gwjob)
        elif when_run.upper() == "SUCCESS":
            add_final_job_as_sink(generic_workflow, gwjob)
        else:
            raise ValueError(f"Invalid value for finalJob.whenRun: {when_run}")


def _add_merge_job(config, generic_workflow, prefix):
    """Add job responsible for merging back the execution Butler.

    Depending on configuration, the merge job will be added as a special job
    which will always run regardless of the exit status of the workflow or
    a regular sink node which will only run if the workflow execution finished
    with no errors.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        Bps configuration.
    generic_workflow : `lsst.ctrl.bps.GenericWorkflow`
        Generic workflow to which attributes should be added.
    prefix : `str`
        Directory in which to output final script.
    """
    _, when_create = config.search(".executionButler.whenCreate")
    _, when_merge = config.search(".executionButler.whenMerge")
    if when_create.upper() != "NEVER" and when_merge.upper() != "NEVER":
        create_final_job = _make_final_job_creator("executionButler", _create_merge_command)
        gwjob = create_final_job(config, generic_workflow, prefix)
        if when_merge.upper() == "ALWAYS":
            generic_workflow.add_final(gwjob)
        elif when_merge.upper() == "SUCCESS":
            add_final_job_as_sink(generic_workflow, gwjob)
        else:
            raise ValueError(f"Invalid value for executionButler.whenMerge: {when_merge}")


def _make_final_job_creator(job_name, create_cmd):
    """Construct a function that creates the final job.

    Parameters
    ----------
    job_name : `str`
        Name of the job. It will also be used as the job label.
    create_cmd : callable
        Function to use when creating the script for the final job. It takes
        two positional arguments:

        - `config`: run configuration (`BpsConfig`).
        - `prefix`: directory in which to output final script (`str`).

    Returns
    -------
    create_gwjob : callable
        Function to use to create a generic workflow job. The function takes
        three positional arguments:

        - `config`: run configuration (`BpsConfig`).
        - `generic_workflow`: generic workflow to which the final job should
           be added.
        - `prefix`: directory in which to output final script (`str`).

    Notes
    -----
    Implemented as a closure in order to reduce code duplication and provide
    an extra flexibility needed to support the creation of the final node for
    both the execution and quantum backed Butler with minimal impact on
    the existing code base.  Once all supported plugins are able to use
    the quantum backed Butler the inner function could be merged with
    the remaining function responsible for adding the final node and the
    closure can be removed.
    """

    def create_final_job(config, generic_workflow, prefix):
        gwjob = GenericWorkflowJob(job_name, label=job_name)

        search_opt = {"searchobj": config[job_name], "curvals": {}, "default": None}
        found, value = config.search("computeSite", opt=search_opt)
        if found:
            search_opt["curvals"]["curr_site"] = value
        found, value = config.search("computeCloud", opt=search_opt)
        if found:
            search_opt["curvals"]["curr_cloud"] = value

        # Set job attributes based on the values find in the config excluding
        # the ones in the _ATTRS_MISC group. The attributes in this group are
        # somewhat "special":
        #   * HTCondor plugin, which uses 'attrs' and 'profile', has its own
        #   mechanism for setting them,
        #   * 'cmdvals' is being set internally, not via config.
        job_values = _get_job_values(config, search_opt, None)
        for attr in _ATTRS_ALL - _ATTRS_MISC:
            if not getattr(gwjob, attr) and job_values.get(attr, None):
                setattr(gwjob, attr, job_values[attr])

        # Create script and add command line to job.
        gwjob.executable, gwjob.arguments = create_cmd(config, prefix)

        # Determine inputs from command line.
        #'''zy
        for file_key in re.findall(r"<FILE:([^>]+)>", gwjob.arguments):
            gwfile = generic_workflow.get_file(file_key)
            generic_workflow.add_job_inputs(gwjob.name, gwfile)
        #'''
        _enhance_command(config, generic_workflow, gwjob, {})
        return gwjob

    return create_final_job


def _create_final_command(config, prefix):
    """Create the command and shell script for the final job.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        Bps configuration.
    prefix : `str`
        Directory in which to output final script.

    Returns
    -------
    executable : `lsst.ctrl.bps.GenericWorkflowExec`
        Executable object for the final script.
    arguments : `str`
        Command line needed to call the final script.
    """
    search_opt = {
        "replaceVars": False,
        "replaceEnvVars": False,
        "expandEnvVars": False,
        "searchobj": config["finalJob"],
    }

    script_file = "test.sh"

    executable = GenericWorkflowExec(os.path.basename(script_file), script_file, True)

    _, orig_butler = config.search("butlerConfig")
    return executable, f"<FILE:runQgraphFile> {orig_butler}"
    #return executable, f"{orig_butler}"

def _create_merge_command(config, prefix):
    """Create the command and shell script for merging the execution Butler.

    Parameters
    ----------
    config : `lsst.ctrl.bps.BpsConfig`
        Bps configuration.
    prefix : `str`
        Directory in which to output final script.

    Returns
    -------
    executable : `lsst.ctrl.bps.GenericWorkflowExec`
        Executable object for the final script.
    arguments : `str`
        Command line needed to call the final script.
    """
    search_opt = {
        "replaceVars": False,
        "replaceEnvVars": False,
        "expandEnvVars": False,
        "searchobj": config["executionButler"],
    }

    script_file = os.path.join(prefix, "final_job.bash")
    with open(script_file, "w", encoding="utf8") as fh:
        print("#!/bin/bash\n", file=fh)
        print("set -e", file=fh)
        print("set -x", file=fh)

        print("butlerConfig=$1", file=fh)
        print("executionButlerDir=$2", file=fh)

        i = 1
        found, command = config.search(f"command{i}", opt=search_opt)
        while found:
            # Temporarily replace any env vars so formatter doesn't try to
            # replace them.
            command = re.sub(r"\${([^}]+)}", r"<BPSTMP:\1>", command)

            # executionButlerDir and butlerConfig will be args to script and
            # set to env vars
            command = command.replace("{executionButlerDir}", "<BPSTMP:executionButlerDir>")
            command = command.replace("{butlerConfig}", "<BPSTMP:butlerConfig>")

            # Replace all other vars in command string
            search_opt["replaceVars"] = True
            command = config.formatter.format(command, config, search_opt)
            search_opt["replaceVars"] = False

            # Replace any temporary env placeholders.
            command = re.sub(r"<BPSTMP:([^>]+)>", r"${\1}", command)

            print(command, file=fh)
            i += 1
            found, command = config.search(f"command{i}", opt=search_opt)
    os.chmod(script_file, 0o755)
    executable = GenericWorkflowExec(os.path.basename(script_file), script_file, True)

    _, orig_butler = config.search("butlerConfig")
    # The execution butler was saved as butlerConfig in the workflow.
    return executable, f"{orig_butler} <FILE:butlerConfig>"


def add_final_job_as_sink(generic_workflow, final_job):
    """Add final job as the single sink for the workflow.

    Parameters
    ----------
    generic_workflow : `lsst.ctrl.bps.GenericWorkflow`
        Generic workflow to which attributes should be added.
    final_job : `lsst.ctrl.bps.GenericWorkflowJob`
        Job to add as new sink node depending upon all previous sink nodes.
    """
    # Find sink nodes of generic workflow graph.
    gw_sinks = [n for n in generic_workflow if generic_workflow.out_degree(n) == 0]
    _LOG.debug("gw_sinks = %s", gw_sinks)

    generic_workflow.add_job(final_job)
    generic_workflow.add_job_relationships(gw_sinks, final_job.name)
