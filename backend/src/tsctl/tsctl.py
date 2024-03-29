"""
A Threat Stack rule manager for your terminal.
"""

from typing import Tuple, Dict, Optional

import logging
import configparser
import os
import json

from argparse import ArgumentParser
from textwrap import dedent
from .state import State
from .utils import read_json, write_json
from . import __version__


def config_parse(repository: Optional[str] =None) -> Tuple[str, str, Dict[str, str]]:
    """
    Initialize the state directory in the user's home directory and parse config options.

    Args:
        repository: should only be used to reconfigure configuration with an additional repository layer, if a repo is
            being cloned down. See api.app.clone_git for further details.

    Returns:
        A tuple of the base state directory path (within which all organization changes will be made) and the state
        file path (where these changes will be tracked), as well as API credentials.
    """
    global lazy_eval

    home = os.path.expanduser('~') + '/'
    conf = home + '.threatstack.conf'

    # If this configuration file is not present, write a default one.
    if not os.path.isfile(conf):
        default_conf_file = dedent(
            """
            [RUNTIME]
            LOGLEVEL = DEBUG
            
            [STATE]
            STATE_DIR = .threatstack
            STATE_FILE = .threatstack.state.json
            """
        )[1:]
        with open(conf, 'w') as f:
            f.write(default_conf_file)

    parser = configparser.ConfigParser()
    parser.read(conf)

    # Collect runtime options, such as laziness and log level.
    if 'RUNTIME' in parser.sections():
        runtime_section = parser['RUNTIME']
        loglevel = runtime_section.get('LOGLEVEL', fallback='INFO')

        if logging.getLogger().getEffectiveLevel() != loglevel:
            logging.basicConfig(level=loglevel)
            logging.info(f'Setting log level to \'{loglevel}\'')
    else:
        logging.error(f'Must define RUNTIME section in \'{conf}\'.')
        exit(1)

    # Set up the local state directory and a default state file, if it doesn't exist.
    if 'STATE' in parser.sections():
        state_section = parser['STATE']
        state_directory = state_section.get('STATE_DIR', fallback='.threatstack')
        state_file = state_section.get('STATE_FILE', fallback='.threatstack.state.json')
    else:
        state_directory = '.threatstack'
        state_file = '.threatstack.state.json'

    if repository:
        state_directory += ('/' if not (state_directory.endswith('/') or repository.startswith('/')) else '') + repository

    state_directory_path = home \
        + ('/' if not (home.endswith('/') or state_directory.startswith('/')) else '') \
        + ((state_directory + '/') if not state_directory.endswith('/') else state_directory)
    state_file_path = state_directory_path + state_file

    if not os.path.isdir(state_directory_path):
        logging.debug(f'Making directory \'{state_directory}\' for local state.')
        os.mkdir(state_directory_path)
    else:
        logging.debug(f'Using state directory \'{state_directory_path}\' for local state')

    if not os.path.isfile(state_file_path) or os.path.getsize(state_file_path) < 62:
        # Write the base config to local state.
        logging.debug(f'Initializing state directory tree.')
        write_json(
            state_file_path,
            {
                'workspace': '',
                'organizations': {}
            }
        )
    else:
        # TODO: Ensure the existing file at least conforms to the required schema?
        ...

    # Collect credentials from the rest of the conf or from env.
    if 'CREDENTIALS' in parser.sections():
        credentials = parser['CREDENTIALS']
        if 'USER_ID' not in credentials or 'API_KEY' not in credentials:
            logging.error(f'Must set values for \'USER_ID\' and \'API_KEY\' in \'{conf}\' under CREDENTIALS section.')
            exit(1)
        else:
            credentials = {
                'user_id': credentials['USER_ID'],
                'api_key': credentials['API_KEY']
            }
    else:
        try:
            assert(all(os.getenv(v) is not None for v in ('USER_ID', 'API_KEY')))
        except AssertionError:
            logging.error(f'Must set environment variables for \'USER_ID\' and \'API_KEY\' or define them in \'{conf}\' under CREDENTIALS section.')
            exit(1)
        credentials = {
            'user_id': os.getenv('USER_ID'),
            'api_key': os.getenv('API_KEY')
        }

    return state_directory_path, state_file_path, credentials


def vcs_gitignore(state_dir: str, state_file_name: str) -> None:
    """
    Drop a default `.gitignore` in the state directory to ignore unimportant files in the event a user wants to start
    using VCS.

    Args:
        state_dir: directory all state is stored within (by default, ~/.threatstack/).
        state_file_name: path/filename to give the state file (by default, ~/.threatstack/.threatstack.state.json).

    Returns:
        Nothing.
    """
    files = [
        state_file_name
    ]
    with open(state_dir + '.gitignore', 'w+') as f:
        for file in files:
            f.write(file + '\n')


def workspace(state_dir: str, state_file: str, org_id: str, credentials: Dict[str, str]) -> State:
    """
    Change the current workspace by updating the organization ID in the state file.

    Args:
        state_dir: location of the state directory.
        state_file: location of the state file to be parsed from disk and updated.
        org_id: organization ID to change the current workspace to.
        credentials: if lazy evaluation is disabled and live changes are pushed, credentials are necessary.

    Returns:
        A State object.
    """
    state = read_json(state_file)
    state['workspace'] = org_id
    write_json(state_file, state)
    new_state = State(state_dir, state_file, org_id=org_id, **credentials)
    return new_state


def plan(state_file: str, show: bool =True) -> Optional[Dict]:
    """
    Output a nicely formatted plan of local state and the remote/platform state for the current workspace. This
    function basically just allows you to view the local state file that tracks what is to be pushed, based on the
    last refresh's returns at the organizations' level.

    Args:
        state_file: location of the state file.
        show: if True, show the state file on stdout; otherwise, return the file.

    Returns:
        Nothing.
    """
    if show:
        with open(state_file, 'r') as f:
            print(json.dumps(json.load(f), indent=2))
    else:
        return read_json(state_file)


def main() -> None:
    state_directory, state_file, credentials = config_parse()
    vcs_gitignore(state_directory, state_file.split('/')[-1])

    parser = ArgumentParser(description=__doc__,
                            epilog=f'Remember to commit and push your changes on \'{state_directory}\' to a git repository to maintain version control.')

    # FIXME: there's probably a bug on calls to `add_mutually_exclusive_group` in that required arguments are evaluated
    #  or parser before discerning that more than one flag in the mutually exclusive group was defined.
    group = parser.add_mutually_exclusive_group(required=True)

    # Rules

    group.add_argument(
        '--create-rule', dest='create_rule', nargs=2, type=str, metavar=('RULESET', 'FILE'),
        help='(lazy) Create a new rule from a JSON file.'
    )

    group.add_argument(
        '--copy-rule', dest='copy_rule', nargs=2, type=str, metavar=('RULE', 'RULESET'),
        help='(lazy) Copy a rule from one ruleset to another (in the same organization).'
    )

    group.add_argument(
        '--copy-rule-out', dest='copy_rule_out', nargs=3, type=str, metavar=('RULE', 'RULESET', 'ORGID'),
        help='(lazy) Copy a rule from the current workspace to a ruleset in a different organization.'
    )

    group.add_argument(
        '--update-rule', dest='update_rule', nargs=2, type=str, metavar=('RULE', 'FILE'),
        help='(lazy) Update a rule in a ruleset with a rule in a JSON file.'
    )

    group.add_argument(
        '--update-tags', dest='update_rule_tags', nargs=2, type=str, metavar=('RULE', 'FILE'),
        help='(lazy) Update the tags on a rule.'
    )

    group.add_argument(
        '--delete-rule', dest='delete_rule', nargs=1, type=str, metavar=('RULE',),
        help='(lazy) Delete a rule from the current workspace.'
    )

    # Rulesets

    group.add_argument(
        '--create-ruleset', dest='create_ruleset', nargs=1, type=str, metavar=('FILE',),
        help='(lazy) Create a new ruleset.'
    )

    group.add_argument(
        '--copy-ruleset', dest='copy_ruleset', nargs=1, type=str, metavar=('RULESET',),
        help='(lazy) Copy an entire ruleset with a new name to the same organization.'
    )

    group.add_argument(
        '--copy-ruleset-out', dest='copy_ruleset_out', nargs=2, type=str, metavar=('RULESET', 'ORGID'),
        help='(lazy) Copy an entire ruleset in the current workspace to a different organization.'
    )

    group.add_argument(
        '--update-ruleset', dest='update_ruleset', nargs=2, type=str, metavar=('RULESET', 'FILE'),
        help='(lazy) Update a ruleset from a JSON file.'
    )

    group.add_argument(
        '--delete-ruleset', dest='delete_ruleset', nargs=1, type=str, metavar=('RULESET',),
        help='(lazy) Delete a ruleset from the current workspace.'
    )

    # Workspace options and optional options.

    parser.add_argument(
        '--colorful', dest='color', action='store_true',
        help='Add xterm coloring to output. Only works on certain commands (--list).'
    )

    group.add_argument(
        '--version', dest='version', action='store_true',
        help='Print the version of \'tsctl\'.'
    )

    group.add_argument(
        '-l', '--list', dest='list', action='store_true',
        help='List rulesets and (view) rules'
    )

    group.add_argument(
        '-r', '--refresh', dest='refresh', action='store_true',
        help='Refresh local copy of the organization\'s rules and flush local state.'
    )

    # FIXME: push does not update local ruleset rule lists.
    group.add_argument(
        '--push', dest='push', action='store_true',
        help='Push a workspace\'s state to remote state (the platform).'
    )

    group.add_argument(
        '--plan', dest='plan', action='store_true',
        help=f'View the state file, or the tracked difference between local state and remote state.'
    )

    group.add_argument(
        '-w', '--workspace', dest='switch', type=str, metavar=('ORGID',),
        help='Set the organization ID within which you are working, automatically starts a refresh.'
    )

    options = vars(parser.parse_args())

    if options['version']:
        print(f'tsctl v{__version__}')
        return
    elif options['switch']:
        org_id = options['switch']
        workspace(state_directory, state_file, org_id, credentials)

    state = read_json(state_file)

    if options['plan']:
        plan(state_file)
        return

    org_id = state['workspace']
    if not org_id:
        print('Must set a workspace/organization ID to begin.')
        exit(1)

    organization = State(state_directory, state_file, org_id=org_id, **credentials)

    if options['create_rule']:
        ruleset_id, rule_data = options['create_rule'][0], read_json(options['create_rule'][1])
        organization.create_rule(ruleset_id, rule_data)

    elif options['copy_rule']:
        rule_id, ruleset_id = options['copy_rule']
        organization.copy_rule(rule_id, ruleset_id)

    elif options['copy_rule_out']:
        rule_id, ruleset_id, org_id = options['copy_rule_out']
        organization.copy_rule_out(rule_id, ruleset_id, org_id)

    elif options['update_rule']:
        rule_id, rule_data = options['update_rule'][0], read_json(options['update_rule'][1])
        organization.update_rule(
            rule_id=rule_id,
            rule_data=rule_data
        )

    elif options['update_rule_tags']:
        rule_id, tags_data = options['update_rule_tags'][0], read_json(options['update_rule_tags'][-1])
        organization.create_tags(
            rule_id,
            tags_data
        )

    elif options['delete_rule']:
        rule_id = options['delete_rule'][0]
        organization.delete_rule(rule_id)

    elif options['create_ruleset']:
        ruleset_data = read_json(options['create_ruleset'][0])
        organization.create_ruleset(ruleset_data)

    elif options['copy_ruleset']:
        ruleset_id = options['copy_ruleset'][0]
        organization.copy_ruleset(ruleset_id)

    elif options['copy_ruleset_out']:
        ruleset_id, org_id = options['copy_ruleset_out']
        organization.copy_ruleset_out(ruleset_id, org_id)

    elif options['update_ruleset']:
        ruleset_id, ruleset_data = options['update_ruleset'][0], read_json(options['update_ruleset'][1])
        organization.update_ruleset(ruleset_id, ruleset_data)

    elif options['delete_ruleset']:
        ruleset_id = options['delete_ruleset'][0]
        organization.delete_ruleset(ruleset_id)

    elif options['list']:
        organization.lst(colorful=options['color'])

    elif options['refresh']:
        organization.refresh()

    elif options['push']:
        organization.push()
