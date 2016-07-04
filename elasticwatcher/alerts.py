# -*- coding: utf-8 -*-
import copy
import datetime
import json
import logging
import subprocess
import sys
import warnings
from email.mime.text import MIMEText
from email.utils import formatdate
from smtplib import SMTP
from smtplib import SMTP_SSL
from smtplib import SMTPAuthenticationError
from smtplib import SMTPException
from socket import error

import boto.sns as sns
import requests
import simplejson
from jira.client import JIRA
from jira.exceptions import JIRAError
from requests.exceptions import RequestException
from staticconf.loader import yaml_loader
from util import EAException
from util import elastalert_logger
from util import lookup_es_key
from util import pretty_ts

class BasicMatchString(object):
    """ Creates a string containing fields in match for the given rule. """

    def __init__(self, rule, match):
        self.rule = rule
        self.match = match

    def _ensure_new_line(self):
        while self.text[-2:] != '\n\n':
            self.text += '\n'

    def _add_custom_alert_text(self):
        missing = '<MISSING VALUE>'
        alert_text = unicode(self.rule.get('alert_text', ''))
        if 'alert_text_args' in self.rule:
            alert_text_args = self.rule.get('alert_text_args')
            alert_text_values = [lookup_es_key(self.match, arg) for arg in alert_text_args]

            # Support referencing other top-level rule properties
            # This technically may not work if there is a top-level rule property with the same name
            # as an es result key, since it would have been matched in the lookup_es_key call above
            for i in xrange(len(alert_text_values)):
                if alert_text_values[i] is None:
                    alert_value = self.rule.get(alert_text_args[i])
                    if alert_value:
                        alert_text_values[i] = alert_value

            alert_text_values = [missing if val is None else val for val in alert_text_values]
            alert_text = alert_text.format(*alert_text_values)
        elif 'alert_text_kw' in self.rule:
            kw = {}
            for name, kw_name in self.rule.get('alert_text_kw').items():
                val = lookup_es_key(self.match, name)

                # Support referencing other top-level rule properties
                # This technically may not work if there is a top-level rule property with the same name
                # as an es result key, since it would have been matched in the lookup_es_key call above
                if val is None:
                    val = self.rule.get(name)

                kw[kw_name] = missing if val is None else val
            alert_text = alert_text.format(**kw)

        self.text += alert_text

    def _add_rule_text(self):
        self.text += self.rule['type'].get_match_str(self.match)

    def _add_top_counts(self):
        for key, counts in self.match.items():
            if key.startswith('top_events_'):
                self.text += '%s:\n' % (key[11:])
                top_events = counts.items()

                if not top_events:
                    self.text += 'No events found.\n'
                else:
                    top_events.sort(key=lambda x: x[1], reverse=True)
                    for term, count in top_events:
                        self.text += '%s: %s\n' % (term, count)

                self.text += '\n'

    def _add_match_items(self):
        match_items = self.match.items()
        match_items.sort(key=lambda x: x[0])
        for key, value in match_items:
            if key.startswith('top_events_'):
                continue
            value_str = unicode(value)
            if type(value) in [list, dict]:
                try:
                    value_str = self._pretty_print_as_json(value)
                except TypeError:
                    # Non serializable object, fallback to str
                    pass
            self.text += '%s: %s\n' % (key, value_str)

    def _pretty_print_as_json(self, blob):
        try:
            return simplejson.dumps(blob, sort_keys=True, indent=4, ensure_ascii=False)
        except UnicodeDecodeError:
            # This blob contains non-unicode, so lets pretend it's Latin-1 to show something
            return simplejson.dumps(blob, sort_keys=True, indent=4, encoding='Latin-1', ensure_ascii=False)

    def __str__(self):
        self.text = self.rule['name'] + '\n\n'
        self._add_custom_alert_text()
        self._ensure_new_line()
        if self.rule.get('alert_text_type') != 'alert_text_only':
            self._add_rule_text()
            self._ensure_new_line()
            if self.rule.get('top_count_keys'):
                self._add_top_counts()
            if self.rule.get('alert_text_type') != 'exclude_fields':
                self._add_match_items()
        return self.text
        

class Alerter(object):
    """ Base class for types of alerts.

    :param rule: The rule configuration.
    """
    required_options = frozenset([])

    def __init__(self, rule):
        self.rule = rule

    def create_alert_body(self, matches):
        body = ''
        for match in matches:
            body += unicode(BasicMatchString(self.rule, match))
            # Separate text of aggregated alerts with dashes
            if len(matches) > 1:
                body += '\n----------------------------------------\n'
        return body


class PagerDutyAlerter(Alerter):
    """ Create an incident on PagerDuty for each alert """
    required_options = frozenset(['pagerduty_service_key', 'pagerduty_client_name'])

    def __init__(self, rule):
        super(PagerDutyAlerter, self).__init__(rule)
        self.pagerduty_service_key = self.rule['pagerduty_service_key']
        self.pagerduty_client_name = self.rule['pagerduty_client_name']
        self.pagerduty_incident_key = self.rule.get('pagerduty_incident_key', '')
        self.pagerduty_proxy = self.rule.get('pagerduty_proxy', None)
        self.url = 'https://events.pagerduty.com/generic/2010-04-15/create_event.json'

    def alert(self, matches):
        body = self.create_alert_body(matches)

        # post to pagerduty
        headers = {'content-type': 'application/json'}
        payload = {
            'service_key': self.pagerduty_service_key,
            'description': self.rule['name'],
            'event_type': 'trigger',
            'incident_key': self.pagerduty_incident_key,
            'client': self.pagerduty_client_name,
            'details': {
                "information": body.encode('UTF-8'),
            },
        }

        # set https proxy, if it was provided
        proxies = {'https': self.pagerduty_proxy} if self.pagerduty_proxy else None
        try:
            response = requests.post(self.url, data=json.dumps(payload, ensure_ascii=False), headers=headers, proxies=proxies)
            response.raise_for_status()
        except RequestException as e:
            raise EAException("Error posting to pagerduty: %s" % e)
        elastalert_logger.info("Trigger sent to PagerDuty")
