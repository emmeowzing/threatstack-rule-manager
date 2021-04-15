### Rule Management with `tsctl`

`tsctl` allows you to perform the most common tasks in organization-level rule management, such as

* creating rules and rulesets,
* copying rules, both intra-organizational and extra, and
* 

In addition to the above, since the tool works out of a local directory (by default, 
`~/.threatstack`), it allows for version control with `git`.

## FAQs

### How do I configure the tool?

After installing `tsctl`, you should see one new directory in your home directory, `~/.threatstack`, that stores local state. You may optionally place a
file `~/.threatstack.conf` in your home directory to configure the location and name of the local state directory.

### What does the local state directory's structure look like?

My current view is that the state directory will be structured like ~
```text
~ $ tree -a .threatstack/
.threatstack/
├── 5d7bb7c49f4d069836a064c2
│   ├── 6bd566f5-d63c-11e9-bc18-196d1feb576b
│   │   ├── 6bd69f79-d63c-11e9-bc18-4de0411d891c
│   │   │   ├── rule.json
│   │   │   └── suppressions.json
│   │   └── ruleset.json
│   ├── 6be3e51a-d63c-11e9-bc18-01fe680446ed
│   └── 6c2078d1-d63c-11e9-bc18-1b06bdc4074a
├── .git
├── .gitignore
└── .threatstack.state.json
```
The `.threatstack.state.json` state file tracks local organization and cross-organization changes. This state file is flushed upon pushing local changes. You can view local, uncommitted changes by either typing `git status .` in the configured local state directory or via `tsctl diff`.

### What am I _not_ trying to solve?

### Environment Variables

* `API_KEY`:
* `API_ID`: 
* `LOGLEVEL`: (optional, default: `INFO`)
* `LAZY_EVAL`: (optional, default: `false`)
* `CONFIG_DIR`: (optional, default: `~/.threatstack`)