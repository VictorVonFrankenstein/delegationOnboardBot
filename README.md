# delegationOnboardBot

### Manages delegation of HIVE power to newly created accounts by hiveonboarding
Scans blocks for new comments, votes, transfers and custom_json broadcasted by the new created accounts. When an account
is not able to broadcast a certain amount of comment operations, HIVE power is delegated to this account.

Delegated HP is revoked when:

* the delegation had lasted for a defined amount of days
* the account has sufficient own HP
* the account was muted by an defined account
* the account do not set the referreer as beneficiaries 

### Remarks
* I added referrerAccount to the config, so that delegationAccount can be different from the dapp account
* delegationAccount sends out the transfers (so that only one active key is necessary)
* beneficiaryRemoval is only applied on posts and not on comments
* account activity is only checked for new activity (beginning from time when the bot is started)
* Owned HP is used for maxUserHP 
* The hiveonboard API is used to initialize all referred accounts
* Active delegation is checked by database_api.list_vesting_delegations
* Active key is stored in the beem wallet
* If notifyUser is false, a user will never receive a transfer memo

## Installation of packages for Ubuntu

```
sudo apt-get install python3-pip build-essential libssl-dev python3-dev
```

### Installation of python packages

Clone the git and install the package by
```
pip3 install beem
git clone https://github.com/holgern/delegationonboardbot.git
cd delegationonboardbot
python3 setup.py install
```

### Create a new beem wallet
Create a new wallet and set a wallet password (This password is also stored in the config.json)
```
beempy createwallet
```

Update the Hive nodes and set the blockchain to Hive
```
beempy updatenodes --hive
```

Add the active keys of the delegationAccount to the wallet by
```
beempy addkey
```

#### Running
Create a new config.json, it is possible to copy from config.json.example. Fill out all parameter and run the bot by

```
$ delegationonboardbot /path/to/config.json --datadir=/datadir/ --logconfig=/path/to/logger.json
```

|        Option       | Value                                                |
|:-------------------:|------------------------------------------------------|
| referrerAccount | Referer Account |
| delegationAccount | Account Delegator (can be identically with referrerAccount) |
| adminAccount | Account that monitors program |
| delegationAmount | Amount of delegation to apply to new users (Hive Power) |
| delegationLength | Number of days a delegation lasts by default (0 for infinite)|
| beneficiaryRemoval | Should program remove delegation if user revokes beneficiary (boolean)|
| minPostRC | Total number of comment operations that you’d like to ensure new users can access before delegating|
| muteAccount | Bot will check muting against to automatically remove delegation to abusive users|
| hpWarning | The level of HP available in delegationAccount that should notify adminAccount|
| maxUserHP | Level of HP a referred user will be considered able to support themselves.|
| notifyUser | Should program notify users of delegation updates? (boolean)|
| delegationMsg | Default message sent to users about new delegation|
| delegationLengthMsg | Default message for users who’s delegation revoked after delegationLength expires|
| delegationMuteMsg | Default message for users who’s delegation is revoked due to being muted|
| delegationBeneficiaryMsg | Default message for users who’s delegation is revoked for removing beneficiary under open standard|
| delegationMaxMsg | Default message for users with enough HP to support themselves|
| no_broadcast | When set to true, the bot is not broadcasting and is in a test mode (boolean) |
| print_log_at_block | Defines how often a keep alive log should be printed (block number) |
| wallet_password | The password of the beem wallet, in which the active key of the delegationAccount is stored |


The bot is storing the accounts and its state in the data.db file in the datadir directory. It is possible to stop and start the bot without missing blocks.


## Running the scripts
Adapt path in run-delegationonboardbot.sh
```
chmod a+x run-delegationonboardbot.sh
./run-delegationonboardbot.sh
```
or edit and copy the systemd service file to /etc/systemd/system and start it by
```
systemctl start delegationonboardbot
```