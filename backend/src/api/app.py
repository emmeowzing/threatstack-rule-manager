"""
Provide a slightly-higher level interface between tsctl's state methods and calls and what will be the front end.
"""

from typing import Dict, Optional, cast
from tsctl.state import RuleType

import tsctl
import os
import logging

from http import HTTPStatus
from flask import Flask, redirect, url_for, request, abort
from flask_cors import cross_origin, CORS
from functools import lru_cache
from repo import actions
from concurrent.futures import ThreadPoolExecutor, as_completed


here = os.path.dirname(os.path.realpath(__file__)) + '/'
app = Flask(__name__)
cors = CORS(app)

# Note that this is only called again for reconfiguration if an upstream Git repo is set (cf. clone_git), to ensure that
# tsctl State instances reference the correct path (inside the cloned repo).
state_directory_path, state_file_path, credentials = tsctl.tsctl.config_parse()

cached_read_json = lru_cache(maxsize=32)(tsctl.tsctl.read_json)


# push/refresh thread count.
if 'REMOTE_THREAD_CT' in os.environ:
    thread_count = os.getenv('REMOTE_THREAD_CT')
    try:
        REMOTE_THREAD_CT = int(thread_count)
        logging.info(f'Using {REMOTE_THREAD_CT} threads for push and refresh at the organization-level.')
    except TypeError:
        logging.error(f'Environment variable \'REMOTE_THREAD_CT\' must be an integer >= 1, received value \'{thread_count}\'. Defaulting to no threading.')
        REMOTE_THREAD_CT = None

    if REMOTE_THREAD_CT < 2:
        logging.error(f'Must define a \'REMOTE_THREAD_CT\' value >= 1, received \'{REMOTE_THREAD_CT}\'. Defaulting to no threading.')
        REMOTE_THREAD_CT = None
else:
    # Don't use threading since the environment variable was not set.
    logging.info('\'REMOTE_THREAD_CT\' was not found in env, defaulting to no threading.')
    REMOTE_THREAD_CT = None


def is_workspace_set() -> bool:
    """
    Determine if the user has set a workspace.

    Returns:
        Whether or not, upon reading the state file, the workspace has been set.
    """
    return bool(get_workspace())


def get_workspace() -> str:
    """
    Get the current workspace from the state file.

    Returns:
        Either an emptry string if the workspace has not been set yet, or a string containing the current workspace.
    """
    return tsctl.tsctl.plan(state_file_path, show=False)['workspace']


def new_state(org_id: Optional[str] =None) -> Optional[tsctl.tsctl.State]:
    """
    Create a new State instance.

    Args:
        org_id: if not set, create a new State instance from the current workspace.

    Returns:
        A new State instance.
    """
    if org_id:
        return tsctl.tsctl.State(
            state_directory_path,
            state_file_path,
            **credentials,
            org_id=org_id
        )
    else:
        if is_workspace_set():
            # Create a State instance based on the default read creds.
            org_id = get_workspace()
            return tsctl.tsctl.State(
                state_directory_path,
                state_file_path,
                **credentials,
                org_id=org_id
            )
        else:
            return None


def _ensure_args(request_data: Dict, *args: str) -> bool:
    """
    Take a set of args and a multidict to ensure they reside within.

    Args:
        *args: any number of (required) args to ensure exist in the

    Returns:
        True if all of the args exist and resolve to `True` if tested for bool value, otherwise False.
    """
    return all((arg in request_data and request_data[arg]) for arg in args)


@app.route('/version', methods=['GET'])
@cross_origin()
def version() -> Dict[str, str]:
    """
    Return the version of the backend app (based on tsctl.__version__).
    """
    return {
        "version": tsctl.__version__
    }


@app.route('/templates/ruleset', methods=['GET'])
@cross_origin()
def template_ruleset() -> Dict:
    """
    Get an empty ruleset template.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/ruleset.json')


@app.route('/templates/tags', methods=['GET'])
@cross_origin()
def template_tags() -> Dict:
    """
    Get a skeleton tags JSON template.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/tags.json')


@app.route('/templates/rules/audit', methods=['GET'])
@cross_origin()
def template_rules_audit() -> Dict:
    """
    Get a skeleton audit rule.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/rules/host.json')


@app.route('/templates/rules/cloudtrail', methods=['GET'])
@cross_origin()
def template_rules_cloudtrail() -> Dict:
    """
    Get a skeleton cloudtrail rule.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/rules/cloudtrail.json')


@app.route('/templates/rules/file', methods=['GET'])
@cross_origin()
def template_rules_file() -> Dict:
    """
    Get a skeleton FIM rule.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/rules/file.json')


@app.route('/templates/rules/kubernetesaudit', methods=['GET'])
@cross_origin()
def template_rules_kubernetesaudit() -> Dict:
    """
    Get a skeleton kubernetesAudit rule.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/rules/kubernetes_audit.json')


@app.route('/templates/rules/kubernetesconfig', methods=['GET'])
@cross_origin()
def template_rules_kubernetesconfig() -> Dict:
    """
    Get a skeleton kubernetesAudit rule.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/rules/kubernetes_config.json')


@app.route('/templates/rules/threatintel', methods=['GET'])
@cross_origin()
def template_rules_threatintel() -> Dict:
    """
    Get a skeleton FIM rule.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/rules/threat_intel.json')


@app.route('/templates/rules/winsec', methods=['GET'])
@cross_origin()
def template_rules_winsec() -> Dict:
    """
    Get a skeleton Winsec rule.

    Returns:
        The read template from disk.
    """
    return cached_read_json(f'{here}templates/rules/winsec.json')


@app.route('/plan', methods=['GET'])
@cross_origin()
def plan() -> Dict:
    """
    Get the current state file via tsctl.

    Returns:
        The state file, parsed as JSON.
    """
    return tsctl.tsctl.plan(state_file_path, show=False)


@app.route('/workspace', methods=['GET', 'POST'])
@cross_origin()
def workspace() -> Dict:
    """
    Set the workspace and return a list of rulesets as they appear on-disk. Expects a request payload that looks like

    {
        "workspace": "<org_id>"
    }

    Returns:
        A list of ruleset dictionaries.
    """
    if request.method == 'POST':
        request_data = request.get_json()
        if request_data and _ensure_args(request_data, 'workspace'):
            ws = request_data['workspace']

            # Set or update the workspace in the state file.
            tsctl.tsctl.workspace(state_directory_path, state_file_path, ws, credentials)

            ret = tsctl.tsctl.plan(state_file_path, show=False)
            ret.pop('organizations')
            return ret
    elif request.method == 'GET':
        ret = tsctl.tsctl.plan(state_file_path, show=False)
        ret.pop('organizations')
        return ret
    else:
        abort(HTTPStatus.BAD_REQUEST)


@app.route('/refresh', methods=['POST'])
@cross_origin()
def refresh() -> Dict:
    """
    Refresh an organization's local state. Expects a similar payload as 'push':

    {
        "organizations": [
            "<org_ids>",
            ...
        ]
    }

    If the list is empty, the endpoint defaults to refreshing the current workspace.

    Returns:
        The state file, and if the refresh was successful, the organization's state will be cleared (hence not present).
    """
    def _refresh(org_id: Optional[str] =None) -> None:
        _org = new_state(org_id=org_id)
        _org.refresh()

    request_data = request.get_json()
    if request_data and _ensure_args(request_data, 'organizations'):
        if REMOTE_THREAD_CT and len(request_data['organizations']) > 1:
            # Spawn each refresh in a new thread, since rate limiting is enforced at the organization-level.
            with ThreadPoolExecutor(max_workers=REMOTE_THREAD_CT) as executor:
                futures = (executor.submit(_refresh, org_id) for org_id in request_data['organizations'])
                for future in as_completed(futures):
                    future.result()
        elif len(request_data['organizations']):
            # Single loop.
            for org_id in request_data['organizations']:
                _refresh(org_id)

    elif 'organizations' in request_data and len(request_data['organizations']) == 0 and is_workspace_set():
        # Refresh the current workspace.
        _refresh()

    else:
        abort(HTTPStatus.BAD_REQUEST)

    return tsctl.tsctl.plan(state_file_path, show=False)


@app.route('/push', methods=['POST'])
@cross_origin()
def push() -> Dict:
    """
    Push organizations' local state changes onto the (remote) Threat Stack platform. Expects a similar payload as
    'refresh':

    {
        "organizations": [
            "<org_ids>",
            ...
        ]
    }

    If the list is empty, the endpoint defaults to pushing changes to the current workspace.

    Returns:
        The state file, and if the push was successful, the organization's state will be cleared (hence not present).
    """
    def _push(org_id: Optional[str] =None) -> None:
        _org = new_state(org_id=org_id)
        _org.push()

    request_data = request.get_json()
    if request_data and _ensure_args(request_data, 'organizations'):
        if REMOTE_THREAD_CT and len(request_data['organizations']) > 1:
            # Spawn each push in a new thread, since rate limiting is enforced at the organization-level.
            with ThreadPoolExecutor(max_workers=REMOTE_THREAD_CT) as executor:
                futures = (executor.submit(_push, org_id) for org_id in request_data['organizations'])
                for future in as_completed(futures):
                    future.result()
        elif len(request_data['organizations']):
            # Single loop.
            for org_id in request_data['organizations']:
                _push(org_id)

    elif 'organizations' in request_data and len(request_data['organizations']) == 0 and is_workspace_set():
        # Refresh the current workspace.
        _push()

    else:
        abort(HTTPStatus.BAD_REQUEST)

    return tsctl.tsctl.plan(state_file_path, show=False)


# Git


@app.route('/git/clone', methods=['POST'])
@cross_origin()
def clone_git() -> Dict:
    """
    Clone out a Git repo. Expects an object payload like

    {
        "gitURL": "<repo URL>"
    }

    Returns:
        The contents of the directory once it has been cloned.
    """
    request_data = request.get_json()
    if request_data and _ensure_args(request_data, 'gitURL'):
        # Update the global state directory/file vars, if the repo subdirectory can be extracted from the Git URL.
        global state_directory_path, state_file_path
        git_repo = request_data['gitURL']

        if _repo_state_subdir := actions.initialize_repo(state_directory_path, git_repo):
            # Re-read config file and add the repo's directory to the state file/directory path.
            state_directory_path, state_file_path, _ = tsctl.tsctl.config_parse(_repo_state_subdir)
            return {
                "organizations": os.listdir(state_directory_path)
            }
        else:
            return {
                "error": f"please provide a valid git URL that matches the regular expression '{actions.GIT_REPO_RE.pattern}',"
                         f" or see the API documentation."
            }
    else:
        abort(HTTPStatus.BAD_REQUEST)


@app.route('/git/refresh', methods=['POST'])
@cross_origin()
def refresh_git() -> Dict:
    """
    Essentially the same as `git pull

    Returns:

    """


@app.route('/git/push', methods=['POST'])
@cross_origin()
def push_git() -> Dict:
    """
    Push changes on the master branch to the upstream URL.

    Returns:
        The epoch of the latest push.
    """


@app.route('/git/epochs', methods=['GET'])
@cross_origin()
def epochs_git() -> Dict:
    """
    Get a list of epochs on an organization that a user can checkout and load/query on, such as rules. Expects a
    required search parameter

        ?organization=<org_id1>&organization=<org_id2>...

    Returns:
        A payload that looks like

        {
            <org_id>: [
                <epoch1>
                ...
            ]
            ...
        }

        Since JSON arrays maintain their order, per RFC 7159, we can rely on this to return the epoch list (commits)
        in the desired order for listing in the UI.

        https://datatracker.ietf.org/doc/html/rfc7159#section-1
    """


# Copy


@app.route('/copy', methods=['POST'])
@cross_origin()
def copy() -> Dict:
    """
    Copy either a rule or ruleset intra- or extra-organization.

    {
        "destination_organization": "<optional_organization_ID>",
        "rules": [
            {
                "rule_id": "<rule_ID>",
                "ruleset_id": "<ruleset_ID>",
                "rule_name_postfix": "<optional_postfix>"
            },
            ...
        ],
        "rulesets": [
            {
                "ruleset_id": "<src_ruleset_ID>",
                "ruleset_name_postfix": "<optional_postfix>"
            },
            ...
        ],
        "tags": [
            {
                "src_rule_id": "<src_rule_id>",
                "dst_rule_id": "<dst_rule_id>"
            },
            ...
        ]
    }

    Returns:
        The state file, following the copies.
    """
    if (organization := new_state()) is None:
        return {
            "error": "must set workspace before copying."
        }

    request_data = request.get_json()
    if request_data:
        destination_organization: Optional[str] = None
        if _ensure_args(request_data, 'destination_organization'):
            # All copies should be made to another organization, not to the current workspace.
            destination_organization = request_data['destination_organization']

        if _ensure_args(request_data, 'rules'):
            for rule in request_data['rules']:
                if not _ensure_args(rule, 'rule_id', 'ruleset_id'):
                    return {
                        "error": "rule must have fields 'rule_id' and 'ruleset_id' defined."
                    }
                rule_id, ruleset_id = rule['rule_id'], rule['ruleset_id']
                rule_name_postfix = None
                if _ensure_args(rule, 'rule_name_postfix'):
                    rule_name_postfix = rule['rule_name_postfix']
                if destination_organization:
                    if not organization.copy_rule_out(rule_id, ruleset_id, destination_organization, postfix=rule_name_postfix):
                        return {
                            "error": f"rule ID '{rule_id}' in src organization or ruleset ID '{ruleset_id}' does not exist in dst organization for copying."
                        }
                else:
                    if not organization.copy_rule(rule_id, ruleset_id, postfix=rule_name_postfix):
                        return {
                            "error": f"rule ID '{rule_id}' or ruleset ID '{ruleset_id}' does not exist for copying."
                        }

        if _ensure_args(request_data, 'rulesets'):
            for ruleset_data in request_data['rulesets']:
                if not _ensure_args(ruleset_data, 'ruleset_id'):
                    return {
                        "error": "ruleset must have field 'ruleset_id' defined."
                    }
                ruleset_id = ruleset_data['ruleset_id']
                ruleset_name_postfix = None
                if _ensure_args(ruleset_data, 'ruleset_name_postfix'):
                    ruleset_name_postfix = ruleset_data['ruleset_name_postfix']
                if destination_organization:
                    if not organization.copy_ruleset_out(ruleset_id, destination_organization, postfix=ruleset_name_postfix):
                        return {
                            "error": f"ruleset ID '{ruleset_id}' does not exist in src organization for copying."
                        }
                else:
                    if not organization.copy_ruleset(ruleset_id, postfix=ruleset_name_postfix):
                        return {
                            "error": f"ruleset ID '{ruleset_id}' does not exist for copying."
                        }

        if _ensure_args(request_data, 'tags'):
            for tag in request_data['tags']:
                if not _ensure_args(tag, 'src_rule_id', 'dst_rule_id'):
                    return {
                        "error": "tags must have fields 'src_rule_id' and 'dst_rule_id' defined."
                    }
                src_rule_id, dst_rule_id = tag['src_rule_id'], tag['dst_rule_id']
                if destination_organization:
                    _destination_organization = new_state(org_id=destination_organization)
                    if (tags_data := organization.get_tags(src_rule_id)) is not None:
                        _destination_organization.create_tags(dst_rule_id, tags_data)
                    else:
                        return {
                            "error": f"tags data does not exist on rule ID '{src_rule_id}'."
                        }
                else:
                    if (tags_data := organization.get_tags(src_rule_id)) is not None:
                        organization.create_tags(dst_rule_id, tags_data)
                    else:
                        return {
                            "error": f"tags data does not exist on rule ID '{src_rule_id}'."
                        }

        return tsctl.tsctl.plan(state_file_path, show=False)

    else:
        abort(HTTPStatus.BAD_REQUEST)


# Rules and rulesets (general methods)


@app.route('/rule', methods=['GET', 'PUT', 'POST', 'DELETE'])
@cross_origin()
def rule() -> Dict:
    """
    Depending on the method of request, either

    • Get all rules, with optional filtering capabilities by type, ID, etc. Optional query parameters include

        rule_id=<rule_id>
        rule_type=<rule_type>
        severity=<rule_severity>
        enabled=<true|false>
        tags=<true|false>

    • update a rule in-place,

    {
        "rule_id": "some_rule_id",
        "data": {
            <rule fields>
        }
    }

    • create a new rule entirely,

    {
        "ruleset_id": "<required parent ruleset ID>"
        "data": {
            <rule fields>
        }
    }

    • or, delete a rule, querying by ID(s) (a required field).

        ?rule_id=<rule_id1>&rule_id=<rule_id2>

    where rule_postfix is the postfix to apply to the rule title (defaults to " - COPY"), since rule and ruleset titles
    must be unique in the TS platform.

    Returns:
        The updated state file, minus workspace, to show the update that took place.
    """
    if (organization := new_state()) is None:
        return {
            "error": "must set workspace before you can create rules."
        }

    if request.method == 'GET':
        # Get rule(s') JSON in the current workspace. The following `getlist` calls yield empty lists if there are no
        # key instances present in the args MultiDict instance.
        if not (org_id := get_workspace()):
            return {
                "error": "Must set workspace prior to querying rules."
            }

        rule_ids = request.args.getlist('rule_id')
        rule_type = request.args.get('type')
        rule_severity = request.args.get('severity')
        enabled = request.args.get('enabled')
        tags = request.args.get('tags')

        # Ensure the request args conform to accepted values.
        if rule_ids and rule_type or rule_type and rule_severity or rule_ids and rule_severity:
            return {
                "error": "Cannot specify more than one of 'rule_id', 'rule_type', or 'severity' query parameters."
            }

        # TODO: Somehow make this list importable, or easier to maintain as more rule types are added or removed from
        #  the platform.
        if rule_type and rule_type not in ['file', 'cloudtrail', 'host', 'threatintel', 'windows']:
            return {
                "error": "Rule type can only be one of 'file', 'cloudtrail', 'host', 'threatintel', 'windows'"
            }

        #ret: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {
        #    org_id: {
        #        # Ruleset IDs containing a Dict of rule IDs filtered by the specified parameters.
        #    }
        #}

        if tags:
            if tags not in ['true', 'false']:
                return {
                    "error": "'tags' must be either 'true' or 'false'."
                }
            _tags = True if tags == 'true' else False
        else:
            _tags = False

        if rule_ids:
            ret = organization.lst_api_rules(tags=_tags, rule_ids=rule_ids, full_data=True)
        elif rule_type:
            rule_type = cast(Optional[RuleType], rule_type.lower())
            ret = organization.lst_api_rules(tags=_tags, typ=rule_type, full_data=True)
        elif rule_severity:
            ret = organization.lst_api_rules(tags=_tags, severity=rule_severity, full_data=True)
        else:
            # No filtering by optional (exclusive) fields, just return an entire organization's-worth of rules and
            # containing rulesets.
            ret = organization.lst_api_rules(tags=_tags, full_data=True)

        if not ret:
            return {
                "error": "organization is refreshing, cannot query."
            }

        if enabled:
            if enabled not in ['true', 'false']:
                return {
                    "error": "'enabled' must either be 'true' or 'false."
                }
            if enabled == 'true':
                for ruleset_id in list(ret[org_id]):
                    for rule_id in list(ret[org_id][ruleset_id]['rules']):
                        if not ret[org_id][ruleset_id]['rules'][rule_id]['data']['enabled']:
                            ret[org_id][ruleset_id]['rules'].pop(rule_id)
                            if not ret[org_id][ruleset_id]['rules']:
                                ret[org_id].pop(ruleset_id)
            elif enabled == 'false':
                # Reversed logic on filtering as 'true'.
                for ruleset_id in list(ret[org_id]):
                    for rule_id in list(ret[org_id][ruleset_id]['rules']):
                        if ret[org_id][ruleset_id]['rules'][rule_id]['data']['enabled']:
                            ret[org_id][ruleset_id]['rules'].pop(rule_id)
                            if not ret[org_id][ruleset_id]['rules']:
                                ret[org_id].pop(ruleset_id)

        # Remove empty rulesets due to rule filtering.
        for ruleset_id in list(ret[org_id]):
            if len(ret[org_id][ruleset_id]['rules']) == 0:
                ret[org_id].pop(ruleset_id)

        return ret

    elif request.method == 'PUT':
        # Update the rule in-place.
        request_data = request.get_json()
        if request_data and _ensure_args(request_data, 'rule_id', 'data'):
            rule_id = request_data['rule_id']
            rule_data = request_data['data']
            organization.update_rule(rule_id, rule_data)
        else:
            abort(HTTPStatus.BAD_REQUEST)

    elif request.method == 'POST':
        # Create a new rule.
        request_data = request.get_json()
        if request_data and _ensure_args(request_data, 'ruleset_id', 'data'):
            rule_name_postfix = None
            if _ensure_args(request_data, 'rule_name_postfix'):
                rule_name_postfix = request_data['rule_name_postfix']

            ruleset_id = request_data['ruleset_id']
            data = request_data['data']

            for update in data:
                rule_data = update['rule']
                if 'tags' in update:
                    tags_data = update['tags']
                else:
                    tags_data = None
                organization.create_rule(ruleset_id, rule_data, tags_data, name_postfix=rule_name_postfix)
            return tsctl.tsctl.plan(state_file_path, show=False)
        else:
            abort(HTTPStatus.BAD_REQUEST)

    elif request.method == 'DELETE':
        # Delete rule(s).
        rule_ids = request.args.getlist('rule_id')

        if not rule_ids:
            return {
                "error": "Must submit at least one rule ID to delete."
            }

        for rule_id in rule_ids:
            organization.delete_rule(rule_id)

    return tsctl.tsctl.plan(state_file_path, show=False)


@app.route('/rule/tags', methods=['PUT'])
@cross_origin()
def update_tags() -> Dict:
    """
    Update the tags on a rule in this workspace. Expects a data object similar to the endpoint above.

    {
        "rule_id": "<some_rule_id>",
        "data": {
            "inclusion": [{...}],
            "exclusion": [{...}]
        }
    }

    Returns:
        The updated tags' JSON.
    """
    if (organization := new_state()) is None:
        return {
            "error": "must set workspace before you can update tags."
        }

    request_data = request.get_json()
    if request_data and _ensure_args(request_data, 'rule_id', 'data'):
        rule_id = request_data['rule_id']
        tags_data = request_data['data']
        organization.create_tags(rule_id, tags_data)
    else:
        abort(HTTPStatus.BAD_REQUEST)


@app.route('/ruleset', methods=['GET', 'PUT', 'POST', 'DELETE'])
@cross_origin()
def ruleset() -> Dict:
    """
    Depending on the method of request, either

    • Get a list of rulesets (no rule data, just ruleIds),
    • Update a ruleset's JSON,

    {
        "ruleset_id": "",
        "data": {
            "name": "",
            "description": "",
            "ruleIds": []
        }
    }

    • Create a ruleset,

    <same JSON expected as with the update/POST request>

    • or, delete a ruleset(s) with querying parameters like

        ?ruleset_id=<ruleset_id1>&ruleset_id=<ruleset_id2>

    Returns:
        The updated state file, minus workspace, to show the update that took place.
    """
    if (organization := new_state()) is None:
        return {
            "error": "must set workspace before you can create rulesets."
        }

    if request.method == 'GET':
        # There are no accepted args on this endpoint, so just return a list of all rulesets. The `rule` GET endpoint
        # is a bit more powerful on its searching capabilities.
        if ruleset_data := organization.lst_api_rulesets():
            return ruleset_data
        else:
            return {
                "error": "organization is refreshing, cannot query."
            }

    elif request.method == 'PUT':
        # Update a ruleset.
        request_data = request.get_json()
        if request_data and _ensure_args(request_data, 'data', 'ruleset_id') and _ensure_args(request_data['data'], 'name', 'description', 'ruleIds'):
            ruleset_id = request_data['ruleset_id']
            data = request_data['data']
            if not organization.update_ruleset(ruleset_id, data):
                return {
                    "error": f"ruleset ID '{ruleset_id}' not found."
                }
        else:
            abort(HTTPStatus.BAD_REQUEST)

    elif request.method == 'POST':
        # Create a new ruleset.
        request_data = request.get_json()
        if request_data and _ensure_args(request_data, 'name', 'description', 'ruleIds'):
            ruleset_name = request_data['name']
            ruleset_desc = request_data['description']
            ruleset_rule_ids = request_data['ruleIds']

            data = {
                "name": ruleset_name,
                "description": ruleset_desc,
                "ruleIds": ruleset_rule_ids
            }

            # Read in the optional ruleset name postfix to append if the ruleset name already occurs (to make it a legal
            # ruleset creation).
            ruleset_name_postfix = None
            if _ensure_args(request_data, 'ruleset_name_postfix'):
                ruleset_name_postfix = request_data['ruleset_name_postfix']

            organization.create_ruleset(data, name_postfix=ruleset_name_postfix)
        else:
            abort(HTTPStatus.BAD_REQUEST)

    elif request.method == 'DELETE':
        # Delete ruleset(s).
        ruleset_ids = request.args.getlist('ruleset_id')

        if not ruleset_ids:
            return {
                "error": "Must submit at least one ruleset ID to delete."
            }

        for ruleset_id in ruleset_ids:
            organization.delete_ruleset(ruleset_id)

    return tsctl.tsctl.plan(state_file_path, show=False)


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=8000
    )
