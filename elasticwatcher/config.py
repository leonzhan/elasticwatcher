#!/usr/bin/env python
# -*- coding: utf-8 -*-

import codecs
import copy
import datetime
import hashlib
import json
import logging
import os

import alerts
import rule_types
from util import EAException
from util import elastalert_logger
from util import ts_to_dt
from util import dt_to_ts


# Used to map names of alerts to their classes
alerts_mapping = {
    'pagerduty': alerts.PagerDutyAlerter
}

# Used to map the names of rules to their classes
rules_mapping = {
    'frequency': rule_types.FrequencyRule,
    'any': rule_types.AnyRule,
    'spike': rule_types.SpikeRule,
    'blacklist': rule_types.BlacklistRule,
    'whitelist': rule_types.WhitelistRule,
    'change': rule_types.ChangeRule,
    'flatline': rule_types.FlatlineRule,
    'new_term': rule_types.NewTermsRule,
    'cardinality': rule_types.CardinalityRule,
    "expected": rule_types.ExpectedRule,
    "aggregation_expected": rule_types.AggExpectedRule
}

alerts_order = {
    'jira': 0,
    'email': 1
}

def load_global_config():
    filename = "global_config.json"
    global_config = {}
    try:
        global_config = json.loads(open(filename).read(), encoding="utf-8")
    except ValueError as e:
        raise EAException('Could not parse file %s: %s' % (filename, e))
    return global_config

def load_rules(args):
    """ Creates a conf dictionary for ElastAlerter. Loads the global
    config file and then each rule found in rules folder which contain json formated rule.

    :param args: The parsed arguments to ElastAlert
    :return: The global configuration, a dictionary.
    """

    use_rule = args.rule

    # Load each rule configuration file
    rules = []
    conf = {}
    names = []
    rule_files = get_file_paths(conf, use_rule)
    for rule_file in rule_files:
        
        try:
            rule = load_configuration(rule_file, conf, args)
            if rule['name'] in names:
                raise EAException('Duplicate rule named %s' % (rule['name']))
        except EAException as e:
            raise EAException('Error loading file %s: %s' % (rule_file, e))

        rules.append(rule)
        names.append(rule['name'])

    if not rules:
        logging.exception('No rules loaded. Exiting')
        exit(1)

    conf['rules'] = rules
    return conf


def get_file_paths(conf, use_rule=None):
    # Passing a filename directly can bypass rules_folder and .yaml checks
    if use_rule and os.path.isfile(use_rule):
        return [use_rule]
    rule_folder = "rules"
    rule_files = []
    for root, folders, files in os.walk(rule_folder):
        for filename in files:
            if use_rule and use_rule != filename:
                continue
            if filename.endswith('.json'):
                rule_files.append(os.path.join(root, filename))
    return rule_files


def load_configuration(filename, conf, args=None):
    """ Load a json rule file and fill in the relevant fields with objects.

    :param filename: The name of a rule configuration file.
    :param conf: The global configuration dictionary, used for populating defaults.
    :return: The rule configuration, a dictionary.
    """
    try:
        with codecs.open(filename, 'r', encoding='utf-8') as f:
            rule = json.loads(f.read())
    except ValueError as e:
        raise EAException('Could not parse file %s: %s' % (filename, e))

    rule['rule_file'] = filename

    load_options(rule, args)
    load_modules(rule, args)
    return rule

def load_modules(rule, args=None):
    """ Loads things that could be modules. Enhancements, alerts and rule type. """

    # Convert rule type into RuleType object
    if rule['type'] in rules_mapping:
        rule['type'] = rules_mapping[rule['type']]
    else:
        rule['type'] = get_module(rule['type'])
        if not issubclass(rule['type'], rule_types.RuleType):
            raise EAException('Rule module %s is not a subclass of RuleType' % (rule['type']))

    # Instantiate rule
    try:
        rule['type'] = rule['type'](rule, args)
    except (KeyError, EAException) as e:
        raise EAException('Error initializing rule %s: %s' % (rule['name'], e))

    # Convert rule type into RuleType object
    rule['actions'] = load_alerts(rule, alert_field=rule['actions'])
    
def load_options(rule, args=None):
    # Set defaults
    rule.setdefault('timestamp_field', 'timestamp')
    rule.setdefault('timestamp_type', 'iso')
    rule.setdefault('timestamp_format', '%Y-%m-%dT%H:%M:%SZ')


    # Set timestamp_type conversion function, used when generating queries and processing hits
    rule['timestamp_type'] = rule['timestamp_type'].strip().lower()
    if rule['timestamp_type'] == 'iso':
        rule['ts_to_dt'] = ts_to_dt
        rule['dt_to_ts'] = dt_to_ts
    elif rule['timestamp_type'] == 'unix':
        rule['ts_to_dt'] = unix_to_dt
        rule['dt_to_ts'] = dt_to_unix
    elif rule['timestamp_type'] == 'unix_ms':
        rule['ts_to_dt'] = unixms_to_dt
        rule['dt_to_ts'] = dt_to_unixms
    elif rule['timestamp_type'] == 'custom':
        def _ts_to_dt_with_format(ts):
            return ts_to_dt_with_format(ts, ts_format=rule['timestamp_format'])

        def _dt_to_ts_with_format(dt):
            return dt_to_ts_with_format(dt, ts_format=rule['timestamp_format'])

        rule['ts_to_dt'] = _ts_to_dt_with_format
        rule['dt_to_ts'] = _dt_to_ts_with_format

def load_alerts(rule, alert_field):
    def normalize_config(alert):
        """Alert config entries are either "alertType" or {"alertType": {"key": "data"}}.
        This function normalizes them both to the latter format. """
        if isinstance(alert, basestring):
            return alert, rule
        elif isinstance(alert, dict):
            name, config = iter(alert.items()).next()
            config_copy = copy.copy(rule)
            config_copy.update(config)  # warning, this (intentionally) mutates the rule dict
            return name, config_copy
        else:
            raise EAException()

    def create_alert(alert, alert_config):
        alert_class = alerts_mapping.get(alert) or get_module(alert)
        if not issubclass(alert_class, alerts.Alerter):
            raise EAException('Alert module %s is not a subclass of Alerter' % (alert))
        #missing_options = (rule['type'].required_options | alert_class.required_options) - frozenset(alert_config or [])
        #if missing_options:
        #    raise EAException('Missing required option(s): %s' % (', '.join(missing_options)))
        return alert_class(alert_config)

    try:
        if type(alert_field) != list:
            alert_field = [alert_field]
        alert_field = [normalize_config(x) for x in alert_field]
        alert_field = sorted(alert_field, key=lambda (a, b): alerts_order.get(a, -1))
        # Convert all alerts into Alerter objects
        alert_field = [create_alert(a, b) for a, b in alert_field]

    except (KeyError, EAException) as e:
        raise EAException('Error initiating alert %s: %s' % (rule['actions'], e))

    return alert_field

def get_rule_hashes(conf, use_rule=None):
    rule_files = get_file_paths(conf, use_rule)
    rule_mod_times = {}
    for rule_file in rule_files:
        with open(rule_file) as fh:
            rule_mod_times[rule_file] = hashlib.sha1(fh.read()).digest()
    return rule_mod_times