"""
Provide a high level, lazy, interface that manages local-only edits, and administers a state file as the user
makes changes. State file maintains a list of minimal change to update remote state on `push` for minimal request
count to sync local and platform states.
"""

from typing import Dict, Optional, Callable, Any, Literal, Union, List

import logging
import os
import shutil

from functools import wraps
from urllib.error import URLError
from uuid import uuid4
from .api import API
from .utils import read_json, write_json, Color
from . import lazy_eval


RuleStatus = Literal['rule', 'tags', 'both', 'del']
RulesetStatus = Literal['true', 'false', 'del']
Severity = Literal[1, 2, 3]

# This Literal should match types listed in src/api/templates/rules/.
RuleType = Literal['File', 'CloudTrail', 'Host', 'ThreatIntel', 'Winsec', 'kubernetesAudit', 'kubernetesConfig']


def lazy(f: Callable[..., Optional[Union['State', str]]]) -> Callable[..., Optional[Union['State', str]]]:
    """
    Apply a `push` from local state onto the remote state if the `LAZY_EVAL` environment variable was set to `true`.

    Args:
        f: method on State to optionally automatically apply a push.

    Returns:
        f's normal return, a State instance, if lazy; otherwise, nothing.
    """
    @wraps(f)
    def _new_f(*args: Any, **kwargs: Any) -> Optional['State']:
        if lazy_eval:
            return f(*args, **kwargs)
        else:
            logging.debug(f'Due to lazy eval setting, pushing changes on {f}')
            if (res := f(*args, **kwargs)) is not None:
                res.push()
            return res

    return _new_f


class State:
    """
    Manage local and remote organizational state through OS-level calls and API calls.
    """
    def __init__(self, state_dir: str, state_file: str, user_id: str, api_key: str, postfix: str ='-localonly', *, org_id: str) -> None:
        self.state_dir = state_dir
        self.state_file = state_file
        self.user_id = user_id
        self.api_key = api_key

        self.credentials = {
            'user_id': user_id,
            'api_key': api_key,
            'org_id': org_id
        }

        self.org_id = org_id
        self.organization_dir = state_dir + org_id + '/'

        # Postfix is set on local-only changes and tracked during pushes to the remote platform so that local
        # directories can be assigned their proper platform-assigned UUID.
        self._postfix = postfix

    @property
    def org_id(self) -> str:
        """
        Getter on the current workspace/organization's ID.

        Returns:
            The current workspace we've set.
        """
        return self._org_id

    @org_id.setter
    def org_id(self, value: str) -> None:
        """
        Capture the side affect of adjusting the working directory of this class instance through a property setter.

        Args:
            value: ID to set the current workspace to.

        Returns:
            Nothing.
        """
        self._org_id = value
        self._create_organization(value)

    def push(self) -> None:
        """
        Push local state onto remote platform state. This push occurs organization-by-organization according to the
        local state file by creating an API interface to the remote organization, then POSTing or PUTting local
        tracked changed rulesets and rules.

        Returns:
            Nothing.
        """
        state = read_json(self.state_file)

        if self.org_id in state['organizations']:
            api = API(**self.credentials)

            for ruleset_id in list(state['organizations'][self.org_id]):
                # Check if this ruleset is to just be deleted, since updates and creation depend on a directory
                # structure to be present.
                if state['organizations'][self.org_id][ruleset_id]['modified'] == 'del':
                    try:
                        logging.info(f'Deleting ruleset \'{ruleset_id}\'.')
                        api.delete_ruleset(ruleset_id)
                        state['organizations'][self.org_id].pop(ruleset_id)
                    except URLError as msg:
                        # Continue to next ruleset, deletion as unsuccessful.
                        logging.error(f'Failed to delete ruleset ID \'{ruleset_id}\': {msg}.')
                    continue

                ruleset_dir = f'{self.organization_dir}{ruleset_id}/'
                ruleset_data = read_json(ruleset_dir + 'ruleset.json')
                if ruleset_id.endswith(self._postfix):
                    # It's a new ruleset, the result of a copy or creation. We need to strip out any rules in the
                    # JSON that are `-localonly` as well, because the platform won't know what to do with them until
                    # the ruleset is created.
                    localonly_rules = ruleset_data['ruleIds']
                    ruleset_data['ruleIds'] = []
                    try:
                        logging.info(f'Creating ruleset: {ruleset_id}.')
                        ruleset_response = api.post_ruleset(ruleset_data)
                        new_ruleset_id = ruleset_response['id']
                        logging.debug(f'New ruleset ID: {new_ruleset_id}.')
                    except URLError as msg:
                        # Skip the rule requests, since there's no ruleset to post rules to. User will have to re-run.
                        logging.error(f'Failed to create ruleset ID \'{ruleset_id}\': {msg}.')
                        continue

                    # Now let's go back and append all of these new rules to the ruleset, and update our local records
                    # for effect.
                    for rule_id in localonly_rules:
                        rule_dir = f'{ruleset_dir}{rule_id}/'
                        rule_data = read_json(rule_dir + 'rule.json')
                        tags_data = read_json(rule_dir + 'tags.json')

                        try:
                            logging.info(f'Creating rule ID \'{rule_id}\'.')
                            rule_response = api.post_rule(new_ruleset_id, rule_data)
                            if 'errors' in rule_response:
                                raise URLError(rule_response)
                            state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'tags'
                            new_rule_id = rule_response['id']
                            shutil.move(rule_dir, f'{ruleset_dir}{new_rule_id}/')
                            ruleset_data['ruleIds'].append(new_rule_id)
                        except URLError as msg:
                            logging.error(f'Failed to create rule ID \'{rule_id}\': {msg}.')
                            continue

                        try:
                            logging.info(f'Creating tags on rule ID \'{rule_id}\'.')
                            api.post_tags(new_rule_id, tags_data)
                            if state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'tags':
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'].pop(rule_id)
                            else:
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'rule'
                        except URLError as msg:
                            logging.error(f'Failed to update tags on rule ID \'{rule_id}\': {msg}.')
                            continue

                    # Propagate the assigned ruleset and rule IDs received from the platform back to the directory
                    # structure.
                    new_ruleset_dir = f'{self.organization_dir}{new_ruleset_id}/'
                    shutil.move(ruleset_dir, new_ruleset_dir)
                    write_json(new_ruleset_dir + 'ruleset.json', ruleset_data)

                    if len(state['organizations'][self.org_id][ruleset_id]['ruleIds']) == 0:
                        state['organizations'][self.org_id].pop(ruleset_id)
                    else:
                        # Update the state file to the new ruleset ID, there are still `-localonly` rules, be it a tags
                        # endpoint update, or otherwise, that need to be updated on this new ruleset ID due to failures.
                        state['organizations'][self.org_id][new_ruleset_id] = state['organizations'][self.org_id][ruleset_id]
                        state['organizations'][self.org_id].pop(ruleset_id)
                else:
                    # It's a ruleset that already existed, but there could be `-localonly` (new) rules on it.
                    localonly_rules = [rule_id for rule_id in ruleset_data['ruleIds'] if rule_id.endswith(self._postfix)]
                    ruleset_data['ruleIds'] = [rule_id for rule_id in ruleset_data['ruleIds'] if not rule_id.endswith(self._postfix)]

                    if state['organizations'][self.org_id][ruleset_id]['modified'] == 'true':
                        try:
                            logging.info('Updating ruleset ID \'{ruleset_id}\'.')
                            api.put_ruleset(ruleset_id, ruleset_data)
                            state['organizations'][self.org_id][ruleset_id]['modified'] = 'false'
                        except URLError as msg:
                            # Unlike the other branch, we don't need to skip the rules, because this ruleset does indeed
                            # exist remotely, it just needs to be updated if the POST request fails.
                            logging.error(f'Failed to update ruleset ID \'{ruleset_id}\': {msg}.')
                            pass

                    # Loop over new rules that need to be created in the platform, propagate their new IDs back.
                    for rule_id in localonly_rules:
                        rule_dir = f'{ruleset_dir}{rule_id}/'
                        rule_data = read_json(rule_dir + 'rule.json')
                        tags_data = read_json(rule_dir + 'tags.json')

                        try:
                            logging.info(f'Creating rule ID \'{rule_id}\'.')
                            rule_response = api.post_rule(ruleset_id, rule_data)
                            if 'errors' in rule_response:
                                raise URLError(rule_response)
                            state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'tags'
                            new_rule_id = rule_response['id']
                            shutil.move(rule_dir, f'{ruleset_dir}{new_rule_id}/')
                            ruleset_data['ruleIds'].append(new_rule_id)
                        except URLError as msg:
                            # Request to update failed, remain tracking in state file.
                            logging.error(f'Failed to update rule ID \'{rule_id}\': {msg}')
                            continue

                        try:
                            logging.info(f'Updating tags on rule ID \'{rule_id}\'.')
                            api.post_tags(new_rule_id, tags_data)
                            if state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'tags':
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'].pop(rule_id)
                            else:
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'rule'
                        except URLError as msg:
                            logging.error(f'Failed to update tags on rule ID \'{rule_id}\': {msg}.')
                            continue

                    # Since there are new rule IDs, write this data to the ruleset for tracking.
                    write_json(ruleset_dir + 'ruleset.json', ruleset_data)

                    # Now loop over rules that exist in the platform, but need to be modified in some fashion.
                    for rule_id in list(state['organizations'][self.org_id][ruleset_id]['ruleIds']):
                        rule_dir = f'{ruleset_dir}{rule_id}/'

                        if state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'rule':
                            rule_data = read_json(rule_dir + 'rule.json')
                            try:
                                logging.info(f'Updating rule ID \'{rule_id}\'.')
                                api.put_rule(ruleset_id, rule_id, rule_data)
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'].pop(rule_id)
                            except URLError as msg:
                                logging.error(f'Failed to update rule JSON on rule ID \'{rule_id}\': {msg}.')
                                continue
                        elif state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'tags':
                            tags_data = read_json(rule_dir + 'tags.json')
                            try:
                                logging.info(f'Updating tags on rule ID \'{rule_id}\'.')
                                api.post_tags(rule_id, tags_data)
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'].pop(rule_id)
                            except URLError as msg:
                                logging.error(f'Failed to update tags on rule ID \'{rule_id}\': {msg}.')
                                continue
                        elif state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'both':
                            # Attempt both updates, though this code is kinda duplicated.
                            rule_data = read_json(rule_dir + 'rule.json')

                            try:
                                logging.info(f'Updating rule ID \'{rule_id}\'.')
                                api.put_rule(ruleset_id, rule_id, rule_data)
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'tags'
                            except URLError as msg:
                                logging.error(f'Failed to update rule ID \'{rule_id}\': {msg}.')
                                continue

                            tags_data = read_json(rule_dir + 'tags.json')

                            try:
                                logging.info(f'Updating tags on rule ID \'{rule_id}\'.')
                                api.post_tags(rule_id, tags_data)
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'].pop(rule_id)
                            except URLError as msg:
                                logging.error(f'Failed to update tags on rule ID \'{rule_id}\': {msg}.')
                                continue

                        elif state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'del':
                            try:
                                logging.info(f'Deleting rule ID \'{rule_id}\'.')
                                api.delete_rule(ruleset_id, rule_id)
                                state['organizations'][self.org_id][ruleset_id]['ruleIds'].pop(rule_id)
                            except URLError as msg:
                                # Continue to next rule, deletion was unsuccessful.
                                logging.error(f'Failed on deletion of rule ID \'{rule_id}\': {msg}')
                            continue

                    if state['organizations'][self.org_id][ruleset_id]['modified'] == 'false' and \
                            len(state['organizations'][self.org_id][ruleset_id]['ruleIds']) == 0:
                        state['organizations'][self.org_id].pop(ruleset_id)
            else:
                if len(state['organizations'][self.org_id]) == 0:
                    state['organizations'].pop(self.org_id)
                write_json(self.state_file, state)
        else:
            # Nothing to do.
            return None

    def refresh(self) -> None:
        """
        Effectively `push`'s opposite - instead of pushing local state onto the remote platform stanewname: strte, pull
        all of the remote organization state and copy it over the local organization-level state (effectively
        overwriting the local organization's state). Deletes prior local state and clears state file of organization
        change.

        Returns:
            Nothing.
        """
        # If there's a former failed run of some kind, let's clear the board to ensure we don't mix local state
        # with now untracked local state.
        remote_dir = self.organization_dir + '.remote/'
        backup_dir = self.organization_dir + '.backup/'

        if os.path.isdir(remote_dir):
            # We can just delete this leftover (very likely) partial remote state capture.
            shutil.rmtree(remote_dir)
        if os.path.isdir(backup_dir):
            for ruleset in os.listdir(backup_dir):
                shutil.move(backup_dir + ruleset, self.organization_dir)
            os.rmdir(backup_dir)

        os.mkdir(backup_dir)
        os.mkdir(remote_dir)

        for ruleset in os.listdir(self.organization_dir):
            if ruleset != '.backup' and ruleset != '.remote':
                shutil.move(self.organization_dir + ruleset, backup_dir + ruleset)

        api = API(**self.credentials)

        # Collect rulesets under this organization and create corresponding directories.
        try:
            rulesets = api.get_rulesets()

            if not rulesets:
                logging.error(f'Could not retrieve rulesets from the organization with the provided credentials.')
                return None
            elif 'errors' in rulesets:
                logging.error(f'Could not retrieve organization with the provided credentials: {rulesets["errors"]}.')
                return None

            for ruleset in rulesets['rulesets']:
                ruleset_id = ruleset['id']

                # Remove fields that aren't POSTable from the rulesets' data.
                for field in ('id', 'createdAt', 'updatedAt'):
                    ruleset.pop(field)

                logging.debug(f'Refreshing ruleset ID \'{ruleset_id}\'')

                ruleset_rules = api.get_ruleset_rules(ruleset_id)
                ruleset_dir = remote_dir + ruleset_id + '/'
                os.mkdir(ruleset_dir)
                write_json(ruleset_dir + 'ruleset.json', ruleset)

                for rule in ruleset_rules['ruleIds']:
                    rule_id = rule['id']
                    logging.debug(f'\tPulling rule and tag JSON on rule ID \'{rule_id}\'')
                    rule_tags = api.get_rule_tags(rule_id)
                    rule_dir = ruleset_dir + rule_id + '/'
                    os.mkdir(rule_dir)
                    write_json(rule_dir + 'rule.json', rule)
                    write_json(rule_dir + 'tags.json', rule_tags)
        except (URLError, KeyboardInterrupt):
            # Restore backup, refresh unsuccessful; delete remote state directory.
            logging.error(f'Could not refresh organization {self.org_id} local state, restoring backup')
            shutil.rmtree(remote_dir)
            for ruleset in os.listdir(backup_dir):
                shutil.move(backup_dir + ruleset, self.organization_dir + ruleset)
            else:
                # backup directory should be clear, so let's remove it.
                os.rmdir(backup_dir)
        else:
            # Clear this organization's local state and delete the backup, refresh successful.
            for ruleset in os.listdir(remote_dir):
                shutil.move(remote_dir + ruleset, self.organization_dir + ruleset)
            shutil.rmtree(backup_dir)
            shutil.rmtree(remote_dir)
            self._state_delete_organization(self.org_id)

    # Local state file management API.

    def _state_add_organization(self, state: Optional[Dict] =None) -> Optional[Dict]:
        """
        Add an organization to be tracked in the state file. There should always, however, be a ruleset or rule under
        the organization if it's present, or some work to be done if it's being tracked.

        Args:
            state: Optionally provide an already-opened state file.

        Returns:
            The updated state data if it was provided.
        """
        write_state = not state
        if write_state:
            state = read_json(self.state_file)

        if self.org_id not in state['organizations']:
            state['organizations'][self.org_id] = dict()

        if write_state:
            write_json(self.state_file, state)
            return None
        else:
            return state

    def _state_delete_organization(self, org_id: Optional[str] =None, state: Optional[Dict] =None) -> Optional[Dict]:
        """
        Delete an organization's tracked state in the state file. This method should only ever be called by an
        internal process, such as during a refresh.

        Args:
            org_id: organization ID to pop from the state file's tracking, if it exists.
            state: state file data tp update. If not None, this function will commit changes to disk.

        Returns:
            The updated state data if it was provided.
        """
        write_state = not state
        if write_state:
            state = read_json(self.state_file)

        if org_id:
            if org_id in state['organizations']:
                state['organizations'].pop(org_id)
        else:
            if self.org_id in state['organizations']:
                state['organizations'].pop(self.org_id)

        if write_state:
            write_json(self.state_file, state)
            return None
        else:
            return state

    def _state_add_ruleset(self, ruleset_id: str, action: RulesetStatus, state: Optional[Dict] =None) -> Optional[Dict]:
        """
        Add a ruleset (and organization, if it's not already being tracked) to the state file.

        Args:
            ruleset_id: ruleset to start tracking.
            action: status to set the ruleset to. It must take one of the Literal values defined above.
            state: state file data to update. If not None, this function will commit changes to disk.

        Returns:
            The updated state data if it was provided.
        """
        write_state = not state
        if write_state:
            state = read_json(self.state_file)

        if self.org_id in state['organizations']:
            if ruleset_id in state['organizations'][self.org_id]:
                if action == 'true' and state['organizations'][self.org_id][ruleset_id]['modified'] == 'false':
                    state['organizations'][self.org_id][ruleset_id]['modified'] = 'true'
                elif action != 'del' and state['organizations'][self.org_id][ruleset_id]['modified'] == 'del':
                    # You can't add a deleted ruleset back; it's already been wiped from the state directory.
                    raise ValueError(f'Cannot add ruleset ID \'{ruleset_id}\' back to state file after being deleted.')
                elif action == 'false' and state['organizations'][self.org_id][ruleset_id]['modified'] == 'true':
                    raise ValueError(f'Cannot unmodify a ruleset once it\'s been marked modified.')
            else:
                state['organizations'][self.org_id][ruleset_id] = {
                    'modified': action,
                    'ruleIds': dict()
                }
        else:
            state = self._state_add_organization(state)
            state['organizations'][self.org_id][ruleset_id] = {
                'modified': action,
                'ruleIds': dict()
            }

        if write_state:
            write_json(self.state_file, state)
            return None
        else:
            return state

    def _state_delete_ruleset(self, ruleset_id: str, recursive: bool =False, state: Optional[Dict] =None) -> Optional[Dict]:
        """
        Update the state file to reflect the actions of deleting a ruleset. This method should only be called by
        `_delete_ruleset` or `_state_delete_rule`.

        Args:
            ruleset_id: ruleset on which to set 'modified' to False.
            recursive: only caller using recursive deletion should be `_delete_ruleset`.
            state: state file data to update. If not None, this function will commit changes to disk.

        Returns:
            The updated state data if it was provided.
        """
        write_state = not state
        if write_state:
            state = read_json(self.state_file)

        if self.org_id in state['organizations']:
            if ruleset_id in state['organizations'][self.org_id]:
                if ruleset_id.endswith(self._postfix):
                    state['organizations'][self.org_id].pop(ruleset_id)
                    if len(state['organizations'][self.org_id]) == 0:
                        state['organizations'].pop(self.org_id)
                else:
                    if state['organizations'][self.org_id][ruleset_id]['modified'] != 'del':
                        state['organizations'][self.org_id][ruleset_id]['modified'] = 'del'
                        state['organizations'][self.org_id][ruleset_id]['ruleIds'] = {}
                    else:
                        assert(state['organizations'][self.org_id][ruleset_id]['ruleIds'] == {})
            else:
                state['organizations'][self.org_id][ruleset_id] = {
                    'modified': 'del',
                    'ruleIds': {}
                }
        else:
            # We're starting with a clean state file, as far as this organization is concerned.
            state['organizations'][self.org_id] = {
                ruleset_id: {
                    'modified': 'del',
                    'ruleIds': {}
                }
            }

        if write_state:
            write_json(self.state_file, state)
            return None
        else:
            return state

    def _state_add_rule(self, ruleset_id: str, rule_id: str, endpoint: RuleStatus ='both', state: Optional[Dict] =None) -> Optional[Dict]:
        """
        Add a modified rule to the state file for tracking.

        Args:
            ruleset_id: ruleset within the defined organization to add a rule.
            rule_id: rule ID to add to the state file.
            endpoint: you can further define whether you only want one of the two possible requests to be made.
                By default, 'both' is set to push both 'rule' and 'tags' remotely.
            state: state file data to update. If not None, this function will commit changes to disk.

        Returns:
            The updated state data if it was provided.
        """
        write_state = not state
        if write_state:
            state = read_json(self.state_file)

        if self.org_id in state['organizations']:
            if ruleset_id in state['organizations'][self.org_id]:
                if rule_id in state['organizations'][self.org_id][ruleset_id]['ruleIds']:
                    # This is horribly verbose, but I thought it better for debugging purposes to just spell out all
                    # the possibilities here and the results.
                    if endpoint != 'del' and state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'del':
                        raise ValueError('Cannot modify a deleted rule.')
                    elif endpoint == 'tags' and state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'both':
                        pass
                    elif endpoint == 'rule' and state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'both':
                        pass
                    elif endpoint == 'both' and state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] != 'both':
                        state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'both'
                    elif endpoint == 'tags' and state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'rule':
                        state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'both'
                    elif endpoint == 'rule' and state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] == 'tags':
                        state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'both'
                else:
                    state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = endpoint
            else:
                # Add the ruleset, then the rule thereunder with a recursive call to end up down a different code path.
                state = self._state_add_ruleset(ruleset_id, action='false', state=state)
                state = self._state_add_rule(ruleset_id, rule_id, endpoint, state)
        else:
            # Add the blank organization, then the ruleset and rule thereunder, recursively.
            state = self._state_add_organization(state)
            state = self._state_add_ruleset(ruleset_id, action='false', state=state)
            state = self._state_add_rule(ruleset_id, rule_id, endpoint, state)

        if write_state:
            write_json(self.state_file, state)
            return None
        else:
            return state

    def _state_delete_rule(self, ruleset_id: str, rule_id: str, state: Optional[Dict] =None) -> Optional[Dict]:
        """
        Delete a modified rule from the state file.

        Args:
            ruleset_id: the containing ruleset's ID.
            rule_id: rule ID to delete from local state file in the set organization.
            state: state file data to update. If not None, this function will commit changes to disk.

        Returns:
            The updated state data if it was provided.
        """
        write_state = not state
        if write_state:
            state = read_json(self.state_file)

        if self.org_id in state['organizations']:
            if ruleset_id in state['organizations'][self.org_id]:
                if rule_id in state['organizations'][self.org_id][ruleset_id]['ruleIds']:
                    if rule_id.endswith(self._postfix):
                        state['organizations'][self.org_id][ruleset_id]['ruleIds'].pop(rule_id)
                    elif state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] != 'del':
                            state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'del'
                else:
                    state['organizations'][self.org_id][ruleset_id]['ruleIds'][rule_id] = 'del'
            else:
                # The ruleset isn't yet tracked in state.
                state['organizations'][self.org_id][ruleset_id] = {
                    'modified': 'false',
                    'ruleIds': {
                        rule_id: 'del'
                    }
                }
        else:
            # We're working with a blank state for this organization.
            state['organizations'][self.org_id] = {
                ruleset_id: {
                    'modified': 'false',
                    'ruleIds': {
                        rule_id: 'del'
                    }
                }
            }

        if write_state:
            write_json(self.state_file, state)
            return None
        else:
            return state

    # Local filesystem structure/management API.

    def _locate_rule(self, rule_id: str) -> Optional[str]:
        """
        Since rule IDs are unique per rule (platform-wide, actually), we can return the complete path to a rule by
        ID in the filesystem, if it exists.

        Args:
            rule_id: ID of the rule to obtain the path of.

        Returns:
            The path if the rule's found, otherwise, nothing.
        """
        for ruleset in os.listdir(self.organization_dir):
            if rule_id in os.listdir(self.organization_dir + ruleset):
                rule_dir = f'{self.organization_dir}{ruleset}/{rule_id}/'
                break
        else:
            rule_dir = None

        return rule_dir

    def _locate_ruleset(self, ruleset_id: str) -> Optional[str]:
        """
        Ruleset IDs are also unique; locate a ruleset, if it exists, and return the base path to that ruleset.

        Args:
            ruleset_id: ruleset ID to look up in this organization.

        Returns:
            The base path of the ruleset, if it exists; nothing, otherwise.
        """
        if ruleset_id in os.listdir(self.organization_dir):
            return self.organization_dir + ruleset_id + '/'

    def rule_name_occurs(self, rule_name: str) -> bool:
        """
        Determine if a rule name occurs already in this organization.

        Args:
            rule_name: name to compare against the organization. If a match is found, return True.

        Returns:
            True if the rule name already occurs, False otherwise.
        """
        for ruleset in os.listdir(self.organization_dir):
            ruleset_dir = f'{self.organization_dir}{ruleset}/'
            for rule in os.listdir(ruleset_dir):
                if 'ruleset.json' in rule:
                    continue
                rule_dir = f'{ruleset_dir}{rule}/'
                rule_data = read_json(rule_dir + 'rule.json')
                if rule_name == rule_data['name']:
                    return True
        else:
            return False

    def ruleset_name_occurs(self, ruleset_name: str) -> bool:
        """
        Determine if a ruleset name occurs already in this organization.

        Args:
            ruleset_name: name to compare against the organization. If another match is found, return True.

        Returns:
            True if the ruleset name occurs, False otherwise.
        """
        for ruleset in os.listdir(self.organization_dir):
            ruleset_dir = f'{self.organization_dir}{ruleset}/'
            ruleset_data = read_json(ruleset_dir + 'ruleset.json')
            if ruleset_name == ruleset_data['name']:
                return True
        else:
            return False

    def _create_organization(self, org_id: str) -> None:
        """
        Create a local organization directory if it doesn't already exist, in addition to calling a refresh on that
        directory if that's the case.

        Args:
            org_id: organization ID to create in the local state directory if it does not already.

        Returns:
            True if the organization had to be created, False otherwise.
        """
        if not os.path.isdir(self.state_dir + org_id):
            os.mkdir(self.state_dir + org_id)
            State(self.state_dir, self.state_file, self.user_id, self.api_key, org_id=org_id).refresh()

    def _delete_organization(self, org_id: str) -> None:
        """
        Delete a local organization's directory if it exists.

        Args:
            org_id: organization ID to delete in the local state directory.

        Returns:
            True if there was a deletion, False otherwise.
        """
        if os.path.isdir(self.state_dir + org_id):
            # TODO: investigate whether it'd be better to call `onerror` here
            shutil.rmtree(self.organization_dir, ignore_errors=True)
            self._state_delete_organization(org_id)

        return None

    def _create_ruleset(self, ruleset_data: Dict) -> str:
        """
        Create a local ruleset directory if it doesn't already exist.

        Args:
            ruleset_data: POSTable formatted ruleset data.

        Returns:
            The created ruleset's ID.
        """
        # Generate a temporary UUID.
        while True:
            ruleset_id_gen = str(uuid4()) + self._postfix
            if ruleset_id_gen not in os.listdir(self.organization_dir):
                break

        ruleset_dir = f'{self.organization_dir}{ruleset_id_gen}/'
        os.mkdir(ruleset_dir)
        write_json(ruleset_dir + 'ruleset.json', ruleset_data)

        # Update the state file to track these changes.
        self._state_add_ruleset(ruleset_id_gen, action='true')

        return ruleset_id_gen

    def _edit_ruleset(self, ruleset_id: str, ruleset_data: Dict) -> None:
        """
        Edit a local ruleset that already exists.

        Args
            ruleset_id: ruleset to edit in the local organization's directories.
            ruleset_data: ruleset data to commit to disk.

        Returns:
            Nothing.
        """
        ruleset_dir = f'{self.organization_dir}{ruleset_id}/'
        if not os.path.isdir(ruleset_dir):
            print(f'Ruleset {ruleset_id} doesn\'t exist.')
            return None
        write_json(ruleset_dir + 'ruleset.json', ruleset_data)

        # Update the state file to track these changes.
        self._state_add_ruleset(ruleset_id, action='true')

        return None

    def _delete_ruleset(self, ruleset_id: str) -> None:
        """
        Delete a local ruleset if it exists.

        Args:
            ruleset_id: ruleset to delete in the local organization's directory.

        Returns:
            True if the local deletion was successful, False otherwise.
        """
        ruleset_dir = f'{self.organization_dir}{ruleset_id}/'
        if not os.path.isdir(ruleset_dir):
            print(f'Ruleset {ruleset_id} doesn\'t exist.')
            return None

        shutil.rmtree(ruleset_dir)
        self._state_delete_ruleset(ruleset_id, recursive=True)

    def _create_rule(self, ruleset_id: str, rule_data: Dict, tags_data: Dict) -> Optional[str]:
        """
        Create a local rule directory in a ruleset in an organization's directory.

        Args:
            ruleset_id: ruleset within which to create the rule.
            rule_data: JSON rule data to commit to the generated rule ID's path in `ruleset_id`.
            tags_data: JSON tags data to commit to the generated rule ID's path in `ruleset_id`.

        Returns:
            The created rule's ID.
        """
        ruleset_dir = f'{self.organization_dir}{ruleset_id}/'
        if not os.path.isdir(ruleset_dir):
            logging.error(f'Ruleset {ruleset_id} doesn\'t exist.')
            return None

        # Find a suitable (temporary, local) UUID for this rule; will be updated once the state file has been pushed.
        while True:
            rule_id_gen = str(uuid4()) + self._postfix
            if rule_id_gen not in os.listdir(ruleset_dir):
                break

        rule_dir = f'{ruleset_dir}{rule_id_gen}/'
        os.mkdir(rule_dir)

        write_json(rule_dir + 'rule.json', rule_data)
        write_json(rule_dir + 'tags.json', tags_data)

        # Update the ruleset's contained rule list. This is filtered on `push`, since the `-localonly` rules don't
        # exist yet, so the platform would probably complain.
        ruleset_data = read_json(ruleset_dir + 'ruleset.json')
        ruleset_data['ruleIds'].append(rule_id_gen)
        write_json(ruleset_dir + 'ruleset.json', ruleset_data)

        # Update the state file to track these changes.
        self._state_add_rule(ruleset_id, rule_id_gen, endpoint='both')

        return rule_id_gen

    def _edit_rule(self, rule_id: str, rule_data: Dict) -> None:
        """
        Modify a local rule in a ruleset in an organization's directory.

        Args:
            rule_id: ID of the rule to overwrite data on.
            rule_data: rule data to write to file.

        Returns:
            Nothing.
        """
        if not (rule_dir := self._locate_rule(rule_id)):
            raise ValueError(f'Rule ID \'{rule_id}\' does not exist.')

        write_json(rule_dir + 'rule.json', rule_data)
        ruleset_id = rule_dir.split('/')[-3]
        self._state_add_rule(ruleset_id, rule_id, endpoint='rule')

        return None

    def _delete_rule(self, rule_id: str) -> bool:
        """
        Delete a local rule directory in a ruleset in this organization's directory.

        Args:
            rule_id: rule directory ID to delete from the filesystem.

        Returns:
            True if a rule was deleted (because it existed), False otherwise.
        """
        if not (rule_dir := self._locate_rule(rule_id)):
            print(f'Rule ID \'{rule_id}\' not found in this organization. Please create before updating.')
            return False

        # FIXME: indexing like this is probably going to cause IndexErrors down the road.
        shutil.rmtree(rule_dir)
        ruleset_id = rule_dir.split('/')[-3]
        ruleset_dir = f'{self.organization_dir}{ruleset_id}/'
        self._state_delete_rule(ruleset_id, rule_id)

        # Update the local ruleset's file to reflect this deletion. Rulesets are committed after rules to the platform.
        ruleset_data = read_json(ruleset_dir + 'ruleset.json')
        ruleset_data['ruleIds'].remove(rule_id)
        write_json(ruleset_dir + 'ruleset.json', ruleset_data)

        return True

    def lst(self, colorful: bool =False) -> None:
        """
        List the ruleset and rule hierarchy under an organization, based on local state. This is meant to be
        a more human-readable view of the organization and organization's rules.

        Args:
            colorful: if True, print the output with xterm colors via utils.Color.

        Returns:
            Nothing.
        """
        rulesets = os.listdir(self.organization_dir)
        for ruleset in rulesets:
            ruleset_data = read_json(self.organization_dir + ruleset + '/ruleset.json')
            rule_ids = ruleset_data['ruleIds']
            print(ruleset_data['name'], end='')
            if colorful:
                with Color.blue():
                    print(f'({ruleset})')
            else:
                print(f'({ruleset})')
            for rule_id in rule_ids:
                rule_data = read_json(self.organization_dir + ruleset + '/' + rule_id + '/rule.json')
                print(f'\t{rule_data["name"]} ({rule_data["type"]}) ', end='')
                if colorful:
                    with Color.blue():
                        print(f'({rule_id})')
                else:
                    print(f'({rule_id})')

    def lst_api_rulesets(self) -> Optional[Dict]:
        """
        Provide a list of this organization's ruleset data, minus contained rules. This method, like
        `self.lst_api_rules` (simpler, however), should only be called by the API.

        Returns:
            Either None if the organization is empty, or a dictionary containing this organization's rulesets.
        """
        ruleset_list = os.listdir(self.organization_dir)

        # Ensure this organization isn't going through a refresh.
        if '.remote' in ruleset_list:
            return None

        ret = {
            self.org_id: dict()
        }

        for ruleset_id in ruleset_list:
            ruleset_dir = self.organization_dir + ruleset_id + '/'
            ruleset_data = read_json(ruleset_dir + 'ruleset.json')
            ret[self.org_id][ruleset_id] = ruleset_data

        return ret

    def lst_api_rules(self, tags: bool =False, rule_ids: Optional[List] =None, severity: Optional[Severity] =None, typ: Optional[RuleType] =None, full_data: bool =False) -> Optional[Dict[str, Dict[str, Dict[str, Any]]]]:
        """
        Provide a list of this organization's rulesets and rules to an API call. This method should only be called by
        the API, since the calling method should have additional logic to restrict what args can be provided by a user.

        Args:
            rule_ids: rule IDs to filter the list by, if they occur.
            severity: either 1, 2, or 3.
            typ: rule type to filter the results of the former querying parameters by.
            tags: if True, return rules' tags as well, not just their names (by default, False).
            full_data: if True, return the rule's full data.

        Returns:
            Either None if the organization is empty (either hasn't been refreshed, or is currently going through one),
            or a dictionary containing this organization's rules and rulesets.
        """
        ruleset_list = os.listdir(self.organization_dir)

        # Ensure this organization isn't going through a refresh.
        if '.remote' in ruleset_list:
            return None

        ret = {
            self.org_id: dict()
        }

        for ruleset_id in ruleset_list:
            ruleset_dir = self.organization_dir + ruleset_id + '/'
            ruleset_data = read_json(ruleset_dir + 'ruleset.json')
            ruleset_name = ruleset_data['name']
            ruleset = {
                'name': ruleset_name,
                'ruleIds': dict()
            }
            for rule_id in os.listdir(ruleset_dir):
                if 'ruleset.json' not in rule_id:
                    rule_dir = ruleset_dir + rule_id + '/'
                    rule_data = read_json(rule_dir + 'rule.json')

                    # Filter the rule list by query params (provided from the API request params).
                    if rule_ids and rule_id not in rule_ids:
                        continue
                    elif severity and rule_data['severityOfAlerts'] != severity:
                        continue
                    elif typ and rule_data['type'].lower() != typ:
                        continue

                    if full_data:
                        ruleset['ruleIds'][rule_id] = {}
                        ruleset['ruleIds'][rule_id]['data'] = rule_data
                    else:
                        rule_name = rule_data['name']
                        ruleset['ruleIds'][rule_id] = {}
                        ruleset['ruleIds'][rule_id]['data'] = {}
                        ruleset['ruleIds'][rule_id]['data']['name'] = rule_name

                    if tags:
                        ruleset['ruleIds'][rule_id]['tags'] = read_json(rule_dir + 'tags.json')

            ret[self.org_id][ruleset_id] = ruleset
        return ret

    def get_tags(self, rule_id: str) -> Optional[Dict]:
        """
        Get the tags for a particular rule ID.

        Args:
            rule_id: rule ID on which to return tags.

        Returns:
             The tags' JSON.
        """
        if not (rule_dir := self._locate_rule(rule_id)):
            print(f'Rule ID \'{rule_id}\' not found in this organization. Please create before updating its tags.')
            return None

        tags_data = read_json(rule_dir + 'tags.json')
        return tags_data

    # Remote state management API.

    @lazy
    def create_ruleset(self, ruleset_data: Union[str, Dict], name_postfix: Optional[str] =None) -> str:
        """
        Create a new ruleset in the current workspace.

        Args:
            ruleset_data: ruleset data file with which to create the new ruleset. Must be in POSTable format.
            name_postfix: optionally, specify a rule name postfix to append to guarantee name uniqueness; defaults to
                ' - COPY'.

        Returns:
            The new ID of the ruleset, since it's probably locally generated.
        """
        if ruleset_data is str:
            data = read_json(ruleset_data)
        else:
            data = ruleset_data

        while self.ruleset_name_occurs(data['name']):
            if name_postfix:
                data['name'] += name_postfix
            else:
                data['name'] += ' - COPY'

        return self._create_ruleset(ruleset_data=data)

    @lazy
    def create_rule(self, ruleset_id: str, rule_data: Union[str, Dict], tags_data: Optional[Union[str, Dict]] =None, name_postfix: Optional[str] =None) -> Optional[str]:
        """
        Create a new rule from a JSON file in the current workspace.

        Args:
            ruleset_id: ruleset under which to create the new rule.
            rule_data: rule data file from which to create the new rule. Must conform to the POST rule schema.
            tags_data: optionally, specify the tags this new rule should have.
            name_postfix: optionally, specify a rule name postfix to append to guarantee uniqueness; defaults to
                ' - COPY'.

        Returns:
            The name of the created rule, since it's probably generated.
        """
        if rule_data is str:
            data = read_json(rule_data)
        else:
            data = rule_data

        if tags_data is None:
            tags = dict()
        elif tags_data is str:
            tags = read_json(tags_data)
        else:
            tags = tags_data

        while self.rule_name_occurs(data['name']):
            if name_postfix:
                data['name'] += name_postfix
            else:
                data['name'] += ' - COPY'

        return self._create_rule(
            ruleset_id=ruleset_id,
            rule_data=data,
            tags_data=tags
        )

    @lazy
    def create_tags(self, rule_id: str, tags_data: Dict) -> 'State':
        """
        Create or update the tags on a rule.

        Args:
            rule_id: rule ID on which to modify the tags. This rule must already exist.
            tags_data: data to use in overwriting the current rule's tags.

        Returns:
            A State instance.
        """
        if not (rule_dir := self._locate_rule(rule_id)):
            print(f'Rule ID \'{rule_id}\' not found in this organization. Please create before updating its tags.')
            return self

        write_json(rule_dir + 'tags.json', tags_data)
        # TODO: Instead of indexing this, we should make it more logical. This also risks exceptions.
        ruleset_id = rule_dir.split('/')[-3]
        self._state_add_rule(ruleset_id, rule_id, endpoint='tags')
        return self

    @lazy
    def copy_rule(self, rule_id: str, ruleset_id: str, postfix: Optional[str] =None) -> Optional['State']:
        """
        Copy an existing rule in the current workspace to another ruleset in the same workspace.

        Args:
            rule_id: rule ID to copy.
            ruleset_id: destination ruleset to copy to; must reside in the current organization.
            postfix: optionally, specify a postfix to apply to the copied rule's name to ensure uniqueness. Defaults to ' - COPY'.

        Returns:
            A State instance.
        """
        # Locate the rule in this organization (make sure it exists, that is).
        if not (rule_dir := self._locate_rule(rule_id)):
            logging.error(f'Rule ID \'{rule_id}\' not found in this organization. Please create before updating.')
            return None

        # Ensure the destination ruleset ID exists in this workspace.
        if ruleset_id not in os.listdir(self.organization_dir):
            logging.error(f'Destination ruleset ID \'{ruleset_id}\' not found in this workspace.')
            return None

        rule_data = read_json(rule_dir + 'rule.json')
        tags_data = read_json(rule_dir + 'tags.json')

        while self.rule_name_occurs(rule_data['name']):
            if postfix:
                rule_data['name'] += postfix
            else:
                rule_data['name'] += ' - COPY'

        # Create a new rule in the destination ruleset, now that we've confirmed everything exists.
        self._create_rule(ruleset_id, rule_data, tags_data)

        return self

    @lazy
    def copy_rule_out(self, rule_id: str, ruleset_id: str, org_id: str, postfix: Optional[str] =None) -> Optional['State']:
        """
        Copy an existing rule in the current workspace to another ruleset in a different workspace. This
        will trip a refresh action against the next workspace prior to copying if it doesn't already exist.

        Args:
            rule_id: rule ID to copy.
            ruleset_id: destination ruleset in a different workspace to copy this rule to.
            org_id: a different workspace to copy this rule to.
            postfix: optionally, specify a postfix to apply to the copied rule's name to ensure uniqueness. Defaults to ' - COPY'

        Returns:
            A State instance.
        """
        if not (rule_dir := self._locate_rule(rule_id)):
            print(f'Rule ID \'{rule_id}\' not found in this organization. Please create before updating.')
            return self

        alt_org = State(self.state_dir, self.state_file, self.user_id, self.api_key, org_id=org_id)

        # Ensure the destination ruleset ID exists in the destination organization.
        if ruleset_id not in os.listdir(alt_org.organization_dir):
            logging.error(f'Destination ruleset ID \'{ruleset_id}\' not found in organization \'{org_id}\'. Please create this ruleset first.')
            return None

        rule_data = read_json(rule_dir + 'rule.json')
        tags_data = read_json(rule_dir + 'tags.json')

        while alt_org.rule_name_occurs(rule_data['name']):
            if postfix:
                rule_data['name'] += postfix
            else:
                rule_data['name'] += ' - COPY'

        # Create a local copy of this rule in the destination organization.
        alt_org.create_rule(ruleset_id, rule_data, tags_data)

        return self

    @lazy
    def copy_ruleset(self, ruleset_id: str, postfix: Optional[str] =None) -> Optional['State']:
        """
        Copy an entire ruleset to a new one, intra-org.

        Args:
            ruleset_id: the ruleset ID to copy.
            postfix: optionally, specify a postfix to apply to the copied ruleset's name to ensure uniqueness.

        Returns:
            A State instance.
        """
        if not (ruleset_dir := self._locate_ruleset(ruleset_id)):
            logging.error(f'Ruleset ID \'{ruleset_id}\' not found in this organization. Please create before updating.')
            return None

        ruleset_data = read_json(ruleset_dir + 'ruleset.json')

        while self.ruleset_name_occurs(ruleset_data['name']):
            if postfix:
                ruleset_data['name'] += postfix
            else:
                ruleset_data['name'] += ' - COPY'

        new_ruleset_id = self.create_ruleset(
            ruleset_data,
            postfix
        )

        new_ruleset_dir = self.organization_dir + new_ruleset_id + '/'

        new_rule_ids = []
        for rule_id in os.listdir(ruleset_dir):
            if rule_id == 'ruleset.json':
                # skip this file.
                continue
            rule_dir = ruleset_dir + rule_id + '/'
            rule_data = read_json(rule_dir + 'rule.json')
            tags_data = read_json(rule_dir + 'tags.json')
            new_rule_id = self.create_rule(new_ruleset_id, rule_data)
            new_rule_ids.append(new_rule_id)
            self.create_tags(new_rule_id, tags_data)

        # Update ruleIds list on this new ruleset.
        if new_rule_ids:
            new_ruleset_data = read_json(new_ruleset_dir + 'ruleset.json')
            new_ruleset_data['ruleIds'] = new_rule_ids
            write_json(new_ruleset_dir + 'ruleset.json', new_ruleset_data)

        return self

    @lazy
    def copy_ruleset_out(self, ruleset_id: str, org_id: str, postfix: Optional[str] =None) -> Optional['State']:
        """
        Copy an entire ruleset to a new organization. Process looks like,

        1) create a new State instance around this destination organization,
        2) read off this organization, and
        3) make high-level calls to create rules and rulesets in this destination State instance.

        This way, the state changes to the destination org. are managed by that instance.

        Args:
            ruleset_id: ruleset to copy to the new organization.
            org_id: the destination organization to copy these rules and rulesets into.
            postfix: optionally, specify a postfix to apply to the ruleset's name to ensure uniqueness.

        Returns:
            A State instance.
        """
        if not (ruleset_dir := self._locate_ruleset(ruleset_id)):
            logging.error(f'Ruleset ID \'{ruleset_id}\' not found in this organization. Please create before updating.')
            return None

        alt_org = State(self.state_dir, self.state_file, self.user_id, self.api_key, org_id=org_id)

        ruleset_data = read_json(ruleset_dir + 'ruleset.json')

        # Ensure this ruleset name doesn't duplicate any others in the destination organization.
        while alt_org.ruleset_name_occurs(ruleset_data['name']):
            if postfix:
                ruleset_data['name'] += postfix
            else:
                ruleset_data['name'] += ' - COPY'

        alt_ruleset_id = alt_org.create_ruleset(
            ruleset_data,
            postfix
        )

        alt_organization_dir = self.state_dir + org_id + '/'
        alt_ruleset_dir = alt_organization_dir + alt_ruleset_id + '/'

        alt_rule_ids = []
        for rule_id in os.listdir(ruleset_dir):
            if rule_id == 'ruleset.json':
                # skip this file.
                continue
            rule_dir = ruleset_dir + rule_id + '/'
            rule_data = read_json(rule_dir + 'rule.json')
            tags_data = read_json(rule_dir + 'tags.json')
            alt_rule_id = alt_org.create_rule(alt_ruleset_id, rule_data)
            alt_rule_ids.append(alt_rule_id)
            alt_org.create_tags(alt_rule_id, tags_data)

        # Update ruleIds list on the ruleset that was created on the alt organization.
        if alt_rule_ids:
            alt_ruleset_data = read_json(alt_ruleset_dir + 'ruleset.json')
            alt_ruleset_data['ruleIds'] = alt_rule_ids
            write_json(alt_ruleset_dir + 'ruleset.json', alt_ruleset_data)

        return self

    @lazy
    def update_rule(self, rule_id: str, rule_data: Dict) -> Optional['State']:
        """
        Update a rule that already exists in the local filesystem.

        Args:
            rule_id: ID of the rule to update.
            rule_data: Data to overwrite the current rule's JSON stashed on-disk.

        Returns:
            A State instance.
        """
        # Locate the rule in this organization (make sure it exists, that is).
        if not (rule_dir := self._locate_rule(rule_id)):
            logging.error(f'Rule ID \'{rule_id}\' not found in this organization. Please create before updating.')
            return None

        ruleset_id = rule_dir.split('/')[-3]
        write_json(rule_dir + 'rule.json', rule_data)
        self._state_add_rule(ruleset_id, rule_id, endpoint='rule')
        return self

    @lazy
    def update_ruleset(self, ruleset_id: str, ruleset_data: Dict) -> Optional['State']:
        """
        Update a ruleset.

        Returns:
            A State instance.
        """
        if not (ruleset_dir := self._locate_ruleset(ruleset_id)):
            logging.error(f'Ruleset ID \'{ruleset_id}\' not found in this organization. Please create before updating.')
            return None

        write_json(ruleset_dir + 'ruleset.json', ruleset_data)
        self._state_add_ruleset(ruleset_id, action='true')
        return self

    @lazy
    def delete_rule(self, rule_id: str) -> 'State':
        """
        Delete a rule from the current workspace.

        Args:
            rule_id: ID of the rule to delete.

        Returns:
            A State instance.
        """
        self._delete_rule(rule_id)

        return self

    @lazy
    def delete_ruleset(self, ruleset_id: str) -> 'State':
        """
        Delete an entire ruleset from the current workspace.

        Args:
            ruleset_id: ruleset ID to delete.

        Returns:
            A State instance.
        """
        self._delete_ruleset(ruleset_id)

        return self
