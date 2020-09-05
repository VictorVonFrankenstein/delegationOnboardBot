#!/usr/bin/python
from beem import Hive
from beem.comment import Comment
from beem.account import Account
from beem.amount import Amount
from beem.blockchain import Blockchain
from beem.nodelist import NodeList
from beem.exceptions import ContentDoesNotExistsException
from beem.utils import addTzInfo, resolve_authorperm, construct_authorperm, derive_permlink, formatTimeString
from datetime import datetime, timedelta, date
from beem.rc import RC
import time
import shelve
import json
import logging
import logging.config
import argparse
import os
import sys
from delegationonboardbot.utils import print_block_log, check_config, store_data, read_data
import requests

logger = logging.getLogger(__name__)


def setup_logging(
    default_path='logging.json',
    default_level=logging.INFO
):
    """Setup logging configuration

    """
    path = default_path
    if os.path.exists(path):
        with open(path, 'rt') as f:
            config = json.load(f)
        logging.config.dictConfig(config)
    else:
        logger.setLevel(default_level)
        logging.basicConfig()        


class DelegationOnboardBot:
    def __init__(self, config, data_file, hived_instance):
        self.config = config
        self.data_file = data_file
        data_db = read_data(data_file)
        if "accounts" in data_db:
            accounts = data_db["accounts"]
        else:
            accounts = {}        
        self.hive = hived_instance
        
        # add log stats
        self.log_data = {"start_time": 0, "last_block_num": None, "new_commands": 0, "stop_block_num": 0,
                         "stop_block_num": 0, "time_for_blocks": 0} 
        config_cnt = 0
        necessary_fields = ["delegationAccount", "referrerAccount", "adminAccount", "delegationAmount", "delegationLength",
                            "beneficiaryRemoval", "minPostRC", "muteAccount", "hpWarning", "maxUserHP",
                            "notifyUser", "delegationMsg", "delegationLengthMsg",
                            "delegationMuteMsg", "delegationBeneficiaryMsg",
                            "delegationMaxMsg"]
        
        check_config(self.config, necessary_fields, self.hive)
        self.hive.wallet.unlock(self.config["wallet_password"])           
        self.onboard_api = "https://hiveonboard.com/api/referrer/%s" % self.config["referrerAccount"]
        self.blockchain = Blockchain(mode='head', blockchain_instance=self.hive)
        self.muted_acc = Account(self.config["muteAccount"], blockchain_instance=self.hive)
        self.delegation_acc = Account(self.config["delegationAccount"], blockchain_instance=self.hive)
        self.muted_accounts = self.muted_acc.get_mutings(limit=1000)

        active_key = False
        for key in self.delegation_acc["active"]["key_auths"]:
            if key[0] in self.hive.wallet.getPublicKeys(current=True):
                active_key = True
        for key in self.delegation_acc["owner"]["key_auths"]:
            if key[0] in self.hive.wallet.getPublicKeys(current=True):
                active_key = True            
        if not active_key:
            logger.warn("Active key from %s is not stored into the beempy wallet." % self.delegation_acc["name"])

        rc = RC(blockchain_instance=self.hive)
        self.comment_rc_costs = rc.comment(tx_size=4000, permlink_length=40, parent_permlink_length=0)
        self.accounts = self.get_referrer(accounts)
        self.update_delegations()
        self.check_muted(self.muted_accounts)
        self.check_delegation_age()
        self.check_max_hp()
        self.print_account_info()
        store_data(self.data_file, "accounts", self.accounts)

    def print_account_info(self):
        revoked = 0
        delegated = 0
        delegated_hp = 0
        for acc in self.accounts:
            if self.accounts[acc]["delegated_hp"] > 0:
                delegated += 1
                delegated_hp += self.accounts[acc]["delegated_hp"]
            if self.accounts[acc]["delegation_revoked"] > 0:
                revoked += 1
        logger.info("%d accounts have been created with referrer %s" % (len(self.accounts), self.config["referrerAccount"]))
        logger.info("%d accounts have received a delegation (%.3f HP)" % (delegated, delegated_hp))
        logger.info("%d accounts have been revoked" % revoked)

    def get_referrer(self, accounts):
        limit = 20
        offset = 0
        last_result = []
        cnt = 0
        result = []
        while last_result is not None and len(last_result) == limit or cnt == 0:
            cnt += 1        
            r = requests.get(self.onboard_api + '?offset=%d' % (offset))
            if r.ok:
                last_result = r.json()["items"]
                if last_result is not None and len(last_result) > 0:
                    result += last_result
                    offset += limit
        for r in result:
            if r["account"] in accounts:
                continue
            accounts[r["account"]] = {"timestamp": None, "weight": None, "muted": False, "rc": 0, "hp": 0,
                                      "delegated_hp": 0, "delegation_timestamp": None, "rc_comments": 0,
                                      "delegation_revoked": False}
            accounts[r["account"]]["timestamp"] = datetime.utcfromtimestamp(float(r["timestamp"]) / 1000.0)
            accounts[r["account"]]["weight"] = r["weight"]
        return accounts

    def update_delegations(self):
        delegations = self.delegation_acc.get_vesting_delegations(start_account='', limit=1000, account=None)
        for d in delegations:
            if d["delegatee"] in self.accounts:
                self.accounts[d["delegatee"]]["delegated_hp"] = self.hive.vests_to_hp(float(Amount(d["vesting_shares"], blockchain_instance=self.hive)))
                self.accounts[d["delegatee"]]["delegation_timestamp"] = formatTimeString(d["min_delegation_time"]).replace(tzinfo=None)

    def check_max_hp(self):
        if self.config["maxUserHP"] <= 0:
            return
        for account in self.accounts:
            if self.accounts[account]["delegated_hp"] == 0:
                continue
            if self.accounts[account]["delegation_revoked"]:
                continue
            if self.accounts[account]["hp"] > self.config["maxUserHP"]:
                self.remove_delegation(account)
                self.notify_account(account, self.config["delegationMaxMsg"])

    def check_delegation_age(self):
        if self.config["delegationLength"] <= 0:
            return
        for account in self.accounts:
            if self.accounts[account]["delegated_hp"] == 0:
                continue
            if self.accounts[account]["delegation_revoked"]:
                continue
            if (datetime.utcnow() - self.accounts[account]["delegation_timestamp"]).total_seconds() / 60 / 60 / 24 > self.config["delegationLength"]:
                self.remove_delegation(account)
                self.notify_account(account, self.config["delegationLengthMsg"])

    def check_muted(self, muted_accounts):
        for acc in muted_accounts:
            if acc not in self.accounts:
                continue
            if not self.accounts[acc]["muted"]:
                self.accounts[acc]["muted"] = True
                store_data(self.data_file, "accounts", self.accounts)
                if self.accounts[acc]["delegated_hp"] > 0 and not self.accounts[acc]["delegation_revoked"]:
                    self.remove_delegation(acc)
                    self.notify_account(acc, self.config["delegationMuteMsg"])

    def notify_admin(self, msg):
        if self.config["no_broadcast"]:
            logger.info("no_broadcast=True, Would send to %s the following message: %s" % (self.config["adminAccount"], msg))
            return
        if self.delegation_acc.blockchain.wallet.locked():
            self.delegation_acc.blockchain.wallet.unlock(self.config["wallet_password"])
        logger.info("Send to %s the following message: %s" % (self.config["adminAccount"], msg))
        self.delegation_acc.transfer(self.config["adminAccount"], 0.001, "HIVE", memo=msg)

    def notify_account(self, account, msg):
        if not self.config["notifyUser"]:
            return
        
        if self.config["no_broadcast"]:
            logger.info("no_broadcast=True, Would send to %s the following message: %s" % (account, msg))
            return
        if self.delegation_acc.blockchain.wallet.locked():
            self.delegation_acc.blockchain.wallet.unlock(self.config["wallet_password"])        
        logger.info("Send to %s the following message: %s" % (account, msg))
        self.delegation_acc.transfer(account, 0.001, "HIVE", memo=msg)

    def check_account_on_activity(self, account, timestamp):
        if account not in self.accounts:
            return
        acc = Account(account, blockchain_instance=self.hive)
        self.accounts[account]["rc"] = acc.get_rc_manabar()["current_mana"]
        self.accounts[account]["hp"] = acc.get_token_power(only_own_vests=True)
        self.accounts[account]["rc_comments"] = self.accounts[account]["rc"] / self.comment_rc_costs
        store_data(self.data_file, "accounts", self.accounts)
        if self.accounts[account]["delegated_hp"] > 0:
            return
        if self.accounts[account]["delegation_revoked"]:
            return
        if self.accounts[account]["hp"] > self.config["maxUserHP"]:
            return
        if self.accounts[account]["rc_comments"] < self.config["minPostRC"]:
            ok = self.add_delegation(account, timestamp)
            if ok:
                self.notify_account(account, self.config["delegationMsg"])

    def check_beneficiaries(self, author, permlink):
        if author not in self.accounts:
            return
        if self.accounts[author]["delegated_hp"] == 0:
            return
        if self.accounts[author]["delegation_revoked"]:
            return
        if not self.config["beneficiaryRemoval"]:
            return
        comment = None
        cnt = 0
        while comment is None and cnt < 10:
            cnt += 1
            try:
                comment = Comment(construct_authorperm(author, permlink), blockchain_instance=self.hive)
            except:
                comment = None
                time.sleep(3)
        referrer_ok = False
        for bene in comment["beneficiaries"]:
            if bene["account"] == self.config["referrerAccount"] and bene["weight"] == self.accounts[author]["weight"]:
                referrer_ok = True
        if not referrer_ok:
            self.remove_delegation(author)
            self.notify_account(author, self.config["delegationBeneficiaryMsg"])

    def check_for_sufficient_hp(self):
        data_db = read_data(self.data_file)
        if "hp_warning_send" in data_db:
            hp_warning_send = data_db["hp_warning_send"]
        else:
            hp_warning_send = False
        hp = self.delegation_acc.get_token_power(only_own_vests=True)
        if hp_warning_send and hp > self.config["hpWarning"]:
            hp_warning_send = False
        elif not hp_warning_send and hp < self.config["hpWarning"]:
            if not self.config["no_broadcast"]:
                hp_warning_send = True
            self.notify_admin("Warning: HIVE POWER of @%s is below %.3f HP" % (self.config["delegationAccount"], self.config["hpWarning"]))
        store_data(self.data_file, "hp_warning_send", hp_warning_send)

    def remove_delegation(self, account):
        
        if self.config["no_broadcast"]:
            logger.info("no_broadcast = True, Would remove delegation from %s" % (account))
            return False
        if self.delegation_acc.blockchain.wallet.locked():
            self.delegation_acc.blockchain.wallet.unlock(self.config["wallet_password"])        
        logger.info("remove delegation from %s" % (account))
        try:
            self.delegation_acc.delegate_vesting_shares(account, 0)
        except Exception as e:
            logger.warn(str(e))
            self.notify_admin("Could not undelegate HP from %s" % (account))
            return False
        self.accounts[account]["delegation_revoked"] = True
        store_data(self.data_file, "accounts", self.accounts)
        return True

    def add_delegation(self, account, timestamp):
        if self.config["no_broadcast"]:
            logger.info("no_broadcast = True, Would add delegation of %.2f HP to %s" % (self.config["delegationAmount"], account))
            return False
        if self.delegation_acc.blockchain.wallet.locked():
            self.delegation_acc.blockchain.wallet.unlock(self.config["wallet_password"])        
        logger.info("add delegation of %.2f HP to %s" % (self.config["delegationAmount"], account))
        try:
            self.delegation_acc.delegate_vesting_shares(account, self.hive.hp_to_vests(self.config["delegationAmount"]))
        except Exception as e:
            logger.warn(str(e))
            self.notify_admin("Could not delegate %.2f HP to %s" % (self.config["delegationAmount"], account))
            return False
        self.accounts[account]["delegated_hp"] = self.config["delegationAmount"]
        self.accounts[account]["delegation_timestamp"] = timestamp
        self.accounts[account]["delegation_revoked"] = False
        store_data(self.data_file, "accounts", self.accounts)
        return True

    def run(self, start_block, stop_block):
        if self.hive.wallet.locked():
            self.hive.wallet.unlock(self.config["wallet_password"])
        if self.hive.wallet.locked():
            logger.error("Could not unlock wallet. Please check wallet_passowrd in config")
            return
                
        current_block = self.blockchain.get_current_block_num()
        if stop_block is None or stop_block > current_block:
            stop_block = current_block
        
        if start_block is None:
            start_block = current_block
            last_block_num = current_block - 1
        else:
            last_block_num = start_block - 1
        
        self.check_delegation_age()
        self.check_max_hp()
        self.check_for_sufficient_hp()
        store_data(self.data_file, "accounts", self.accounts)        

        self.log_data["start_block_num"] = start_block
        for op in self.blockchain.stream(start=start_block, stop=stop_block):
            self.log_data = print_block_log(self.log_data, op, self.config["print_log_at_block"])
            last_block_num = op["block_num"]
            timestamp = op["timestamp"].replace(tzinfo=None)
            
            if op["type"] == "comment":
                account = op["author"]
                if account not in list(self.accounts.keys()):
                    continue
                self.check_account_on_activity(account, timestamp)
                if op["parent_author"] == "":
                    self.check_beneficiaries(op["author"], op["permlink"])
            elif op["type"] == "vote":
                account = op["voter"]
                if account not in list(self.accounts.keys()):
                    continue                
                self.check_account_on_activity(account, timestamp)
            elif op["type"] == "transfer":
                account = op["from"]
                if account not in list(self.accounts.keys()):
                    continue                
                self.check_account_on_activity(account, timestamp)
            elif op["type"] == "custom_json":

                if len(op["required_posting_auths"]) > 0:
                    account = op["required_posting_auths"][0]
                elif len(op["required_auths"]) > 0:
                    account = op["required_auths"][0]
                if op["id"] == "follow":
                    if op["json"] == "":
                        continue
                    json_data = json.loads(op["json"])
                    if "what" not in json_data:
                        continue
                    if len(json_data["what"]) == 0:
                        continue
                    if json_data["what"][0] != "ignore":
                        continue
                    if account == self.config["muteAccount"] and json_data["following"] in self.accounts:
                        self.check_muted([json_data["following"]])
                        
                if account not in list(self.accounts.keys()):
                    continue
                
                self.check_account_on_activity(account, timestamp)
            elif op["type"] == "delegate_vesting_shares":
                if op["delegator"] != self.config["delegationAccount"]:
                    continue
                account = op["delegatee"]
                if account not in list(self.accounts.keys()):
                    continue
                delegated_hp = self.hive.vests_to_hp(float(Amount(op["vesting_shares"], blockchain_instance=self.hive)))
                self.accounts[account]["delegated_hp"] = delegated_hp
                self.accounts[account]["delegation_timestamp"] = timestamp
                if delegated_hp > 0 and self.accounts[account]["delegation_revoked"]:
                    self.accounts[account]["delegation_revoked"] = False
                elif delegated_hp == 0 and not self.accounts[account]["delegation_revoked"]:
                    self.accounts[account]["delegation_revoked"] = True
                store_data(self.data_file, "accounts", self.accounts)

            elif op["type"] == "create_claimed_account":
                if op["json_metadata"] == "":
                    continue
                meta_data = json.loads(op["json_metadata"])
                if "beneficiaries" not in meta_data:
                    continue
                for entry in meta_data["beneficiaries"]:
                    if entry["label"] == "referrer" and entry["name"] == self.config["referrerAccount"]:
                        self.accounts[op["new_account_name"]] = {"timestamp": None, "weight": None, "muted": False, "rc": 0, "hp": 0,
                                                                 "delegated_hp": 0, "delegation_timestamp": None, "rc_comments": 0,
                                                                 "delegation_revoked": False}
                        self.accounts[op["new_account_name"]]["weight"] = entry["weight"]
                        self.accounts[op["new_account_name"]]["timestamp"] = op["timestamp"].replace(tzinfo=None)
                        store_data(self.data_file, "accounts", self.accounts)
                            
        return last_block_num


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Config file in JSON format")
    parser.add_argument("--logconfig", help="Logger Config file in JSON format", default='logger.json')
    parser.add_argument("--datadir", help="Data storage dir", default='.')
    args = parser.parse_args()
    
    setup_logging(default_path=args.logconfig)
    
    logger.info("Loading config: %s" % str(args.config))
    
    config = json.loads(open(os.path.abspath(args.config)).read())
    datadir = args.datadir

    nodelist = NodeList()
    nodelist.update_nodes()
    hive = Hive(node=nodelist.get_hive_nodes(), num_retries=5, call_num_retries=3, timeout=15)
    blockchain = Blockchain(blockchain_instance=hive)
    logger.info(str(hive))
    data_file = os.path.join(datadir, 'data.db')
    bot = DelegationOnboardBot(
        config,
        data_file,
        hive
    )
    
    data_db = read_data(data_file)
    if "last_block_num" in data_db:
        last_block_num = data_db["last_block_num"]
    else:
        last_block_num = 0
    
    if "last_block_num" in data_db:
        start_block = data_db["last_block_num"] + 1
        if start_block == 35922615:
            start_block += 1
        logger.info("Start block_num: %d" % start_block)
        
        stop_block = start_block + 100
        if stop_block > blockchain.get_current_block_num():
            stop_block = blockchain.get_current_block_num()
    else:
        start_block = None
        stop_block = None
    logger.info("starting delegation manager for onboarding..")
    block_counter = None
    last_print_stop_block = stop_block
    while True:
        if start_block is not None and stop_block is not None:
            if last_print_stop_block is not None and stop_block - last_print_stop_block > 1:
                last_print_stop_block = stop_block
        last_block_num = bot.run(start_block, stop_block)
        # Update nodes once a day
        if block_counter is None:
            block_counter = last_block_num
        elif last_block_num - block_counter > 20 * 60 * 24:
            nodelist.update_nodes()
            hive = Hive(node=nodelist.get_hive_nodes(), num_retries=5, call_num_retries=3, timeout=15)
            
            bot.hive = hive
        
        start_block = last_block_num + 1
        
        stop_block = start_block + 100
        if stop_block > blockchain.get_current_block_num():
            stop_block = blockchain.get_current_block_num()        
        
        store_data(data_file, "last_block_num", last_block_num)
        time.sleep(3)

    
if __name__ == "__main__":
    main()
