#!/usr/bin/env python
# -*- coding: utf-8 -*-
import copy
import datetime
import json
import logging
import os
import signal
import sys
import time
import thread
import threading
import traceback
from socket import error

import argparse
import dateutil.tz
from croniter import croniter
from elasticsearch import RequestsHttpConnection
from elasticsearch.client import Elasticsearch
from elasticsearch.exceptions import ElasticsearchException
import schedule
from config import load_rules
from config import get_rule_hashes
from config import load_global_config 
from util import add_raw_postfix
from util import cronite_datetime_to_timestamp
from util import dt_to_ts
from util import EAException
from util import elastalert_logger
from util import format_index
from util import lookup_es_key
from util import pretty_ts
from util import seconds
from util import set_es_key
from util import ts_add
from util import ts_now
from util import ts_to_dt
from util import unix_to_dt
from util import intervalInSecond


class ElastWatcher():

    def parse_args(self, args):
        parser = argparse.ArgumentParser()
        parser.add_argument('--debug', action='store_true', dest='debug', help='Suppresses alerts and prints information instead')
        parser.add_argument('--rule', dest='rule', help='Run only a specific rule (by filename, must still be in rules folder)')
        parser.add_argument('--silence', dest='silence', help='Silence rule for a time period. Must be used with --rule. Usage: '
                                                              '--silence <units>=<number>, eg. --silence hours=2')
        parser.add_argument('--start', dest='start', help='YYYY-MM-DDTHH:MM:SS Start querying from this timestamp.'
                                                          'Use "NOW" to start from current time. (Default: present)')
        parser.add_argument('--end', dest='end', help='YYYY-MM-DDTHH:MM:SS Query to this timestamp. (Default: present)')
        parser.add_argument('--verbose', action='store_true', dest='verbose', help='Increase verbosity without suppressing alerts')
        parser.add_argument('--pin_rules', action='store_true', dest='pin_rules', help='Stop ElastAlert from monitoring config file changes')
        parser.add_argument('--es_debug', action='store_true', dest='es_debug', help='Enable verbose logging from Elasticsearch queries')
        parser.add_argument('--es_debug_trace', action='store', dest='es_debug_trace', help='Enable logging from Elasticsearch queries as curl command. Queries will be logged to file')
        self.args = parser.parse_args(args)

    def __init__(self, args):
        self.parse_args(args)
        self.debug = self.args.debug
        self.verbose = self.args.verbose
        self.rule_jobs = []

        if self.verbose or self.debug:
            elastalert_logger.setLevel(logging.INFO)

        if self.debug:
            elastalert_logger.info("Note: In debug mode, alerts will be logged to console but NOT actually sent. To send them, use --verbose.")

        if not self.args.es_debug:
            logging.getLogger('elasticsearch').setLevel(logging.WARNING)

        if self.args.es_debug_trace:
            tracer = logging.getLogger('elasticsearch.trace')
            tracer.setLevel(logging.INFO)
            tracer.addHandler(logging.FileHandler(self.args.es_debug_trace))

        self.conf = load_rules(self.args)
        self.global_config = load_global_config()

        #for key,value in self.conf.items():
        #    elastalert_logger.info("%s => %s", key, value)

        #self.max_query_size = self.conf['max_query_size']
        self.rules = self.conf['rules']
        #self.writeback_index = self.conf['writeback_index']
        #self.run_every = self.conf['run_every']
        #self.alert_time_limit = self.conf['alert_time_limit']
        #self.old_query_limit = self.conf['old_query_limit']
        #self.disable_rules_on_error = self.conf['disable_rules_on_error']
        #self.notify_email = self.conf.get('notify_email', [])
        #self.from_addr = self.conf.get('from_addr', 'ElastAlert')
        #self.smtp_host = self.conf.get('smtp_host', 'localhost')
        #self.max_aggregation = self.conf.get('max_aggregation', 10000)
        self.alerts_sent = 0
        self.num_hits = 0
        self.current_es = None
        self.current_es_addr = None
        #self.buffer_time = self.conf['buffer_time']
        self.silence_cache = {}
        self.rule_hashes = get_rule_hashes(self.conf, self.args.rule)
        self.starttime = self.args.start
        self.disabled_rules = []

        #self.es_conn_config = self.build_es_conn_config(self.conf)

        #self.writeback_es = self.new_elasticsearch(self.es_conn_config)

        if self.args.silence:
            self.silence()

    @staticmethod
    def new_elasticsearch(es_conn_conf):
        """ returns an Elasticsearch instance """

        return Elasticsearch(host=es_conn_conf['marvel_es_host'],
                             port=es_conn_conf['marvel_es_port'])
            
    @staticmethod
    def get_index(rule, starttime=None, endtime=None):
        """ Gets the index for a rule. If strftime is set and starttime and endtime
        are provided, it will return a comma seperated list of indices. If strftime
        is set but starttime and endtime are not provided, it will replace all format
        tokens with a wildcard. """
        index = rule['index']
        if rule.get('use_strftime_index'):
            if starttime and endtime:
                return format_index(index, starttime, endtime)
            else:
                # Replace the substring containing format characters with a *
                format_start = index.find('%')
                format_end = index.rfind('%') + 2
                return index[:format_start] + '*' + index[format_end:]
        else:
            return index

    @staticmethod
    def get_query(filters, starttime=None, endtime=None, sort=True, timestamp_field='@timestamp', to_ts_func=dt_to_ts, desc=False):
        """ Returns a query dict that will apply a list of filters, filter by
        start and end time, and sort results by timestamp.

        :param filters: A list of elasticsearch filters to use.
        :param starttime: A timestamp to use as the start time of the query.
        :param endtime: A timestamp to use as the end time of the query.
        :param sort: If true, sort results by timestamp. (Default True)
        :return: A query dictionary to pass to elasticsearch.
        """
        starttime = to_ts_func(starttime)
        endtime = to_ts_func(endtime)
        filters = copy.copy(filters)
        es_filters = {'filter': {'bool': {'must': filters}}}
        if starttime and endtime:
            es_filters['filter']['bool']['must'].insert(0, {'range': {timestamp_field: {'gt': starttime,
                                                                                        'lte': endtime}}})
        query = {'query': {'filtered': es_filters}}
        if sort:
            query['sort'] = [{timestamp_field: {'order': 'desc' if desc else 'asc'}}]
        return query

    def get_terms_query(self, query, size, field):
        """ Takes a query generated by get_query and outputs a aggregation query """
        query = query['query']
        if 'sort' in query:
            query.pop('sort')
        query['filtered'].update({'aggs': {'counts': {'terms': {'field': field, 'size': size}}}})
        aggs_query = {'aggs': query}
        return aggs_query

    def get_index_start(self, index, timestamp_field='@timestamp'):
        """ Query for one result sorted by timestamp to find the beginning of the index.

        :param index: The index of which to find the earliest event.
        :return: Timestamp of the earliest event.
        """
        query = {'sort': {timestamp_field: {'order': 'asc'}}}
        try:
            res = self.current_es.search(index=index, size=1, body=query, _source_include=[timestamp_field], ignore_unavailable=True)
        except ElasticsearchException as e:
            self.handle_error("Elasticsearch query error: %s" % (e), {'index': index})
            return '1969-12-30T00:00:00Z'
        if len(res['hits']['hits']) == 0:
            # Index is completely empty, return a date before the epoch
            return '1969-12-30T00:00:00Z'
        return res['hits']['hits'][0][timestamp_field]

    @staticmethod
    def process_hits(rule, hits):
        """ Update the _source field for each hit received from ES based on the rule configuration.

        This replaces timestamps with datetime objects,
        folds important fields into _source and creates compound query_keys.

        :return: A list of processed _source dictionaries.
        """

        processed_hits = []
        for hit in hits:
            # Merge fields and _source
            hit.setdefault('_source', {})
            for key, value in hit.get('fields', {}).items():
                # Fields are returned as lists, assume any with length 1 are not arrays in _source
                # Except sometimes they aren't lists. This is dependent on ES version
                hit['_source'].setdefault(key, value[0] if type(value) is list and len(value) == 1 else value)

            # Convert the timestamp to a datetime
            #ts = lookup_es_key(hit['_source'], rule['timestamp_field'])
            #set_es_key(hit['_source'], rule['timestamp_field'], rule['ts_to_dt'](ts))
            #set_es_key(hit, rule['timestamp_field'], lookup_es_key(hit['_source'], rule['timestamp_field']))

            # Tack metadata fields into _source
            for field in ['_id', '_index', '_type']:
                if field in hit:
                    hit['_source'][field] = hit[field]

            if rule.get('compound_query_key'):
                values = [lookup_es_key(hit['_source'], key) for key in rule['compound_query_key']]
                hit['_source'][rule['query_key']] = ', '.join([unicode(value) for value in values])

            processed_hits.append(hit['_source'])

        return processed_hits

    def get_hits(self, rule, index):
        """ Query elasticsearch for the given rule and return the results.

        :param rule: The rule configuration.
        :return: A list of hits, bounded by rule['max_query_size'].
        """

        # Parse rule and prepare query criteria like size, sort, from
        query = rule["input"]["search"]["request"]["body"]
        extra_args = {}
        try:
            res = self.current_es.search(index=index, body=query, **extra_args)
            #elastalert_logger.info("=================query result==================================")
            #for key,value in res.items():
            #    elastalert_logger.info("%s => %s", key, value)
        except ElasticsearchException as e:
        
            return None

        hits = res['hits']['hits']
        self.num_hits += len(hits)

        elastalert_logger.info("Queried rule %s: %s hits" % (rule['name'], len(hits)))
        hits = self.process_hits(rule, hits)

        # Record doc_type for use in get_top_counts
        if 'doc_type' not in rule and len(hits):
            rule['doc_type'] = hits[0]['_type']
        return hits

    def run_query(self, rule):
        """ Query for the rule and pass all of the results to the RuleType instance.

        :param rule: The rule configuration.
        Returns True on success and False on failure.
        """

        rule_inst = rule['type']

        # Reset hit counter and query
        prev_num_hits = self.num_hits
        index = rule["input"]["search"]["request"]["indices"]
        data = self.get_hits(rule, index)

        # There was an exception while querying
        if data is None:
            return False
        elif data:
            rule_inst.add_data(data)
        
        return True

    
    
    def alert(self, matches, rule, alert_time=None):
        """ Wraps alerting, kibana linking and enhancements in an exception handler """
        try:
            return self.send_alert(matches, rule, alert_time=alert_time)
        except Exception as e:
            self.handle_uncaught_exception(e, rule)

    def send_alert(self, matches, rule, alert_time=None):
        """ Send out an alert.

        :param matches: A list of matches.
        :param rule: A rule configuration.
        """
        if alert_time is None:
            alert_time = ts_now()

        # Compute top count keys
        if rule.get('top_count_keys'):
            for match in matches:
                if 'query_key' in rule and rule['query_key'] in match:
                    qk = match[rule['query_key']]
                else:
                    qk = None
                start = ts_to_dt(match[rule['timestamp_field']]) - rule.get('timeframe', datetime.timedelta(minutes=10))
                end = ts_to_dt(match[rule['timestamp_field']]) + datetime.timedelta(minutes=10)
                keys = rule.get('top_count_keys')
                counts = self.get_top_counts(rule, start, end, keys, qk=qk)
                match.update(counts)

        # Generate a kibana3 dashboard for the first match
        if rule.get('generate_kibana_link') or rule.get('use_kibana_dashboard'):
            try:
                if rule.get('generate_kibana_link'):
                    kb_link = self.generate_kibana_db(rule, matches[0])
                else:
                    kb_link = self.use_kibana_link(rule, matches[0])
            except EAException as e:
                self.handle_error("Could not generate kibana dash for %s match: %s" % (rule['name'], e))
            else:
                if kb_link:
                    matches[0]['kibana_link'] = kb_link

        if rule.get('use_kibana4_dashboard'):
            kb_link = self.generate_kibana4_db(rule, matches[0])
            if kb_link:
                matches[0]['kibana_link'] = kb_link

        #if not rule.get('run_enhancements_first'):
        #    for enhancement in rule.get('match_enhancements'):
        #        valid_matches = []
        #        for match in matches:
        #            try:
        #                enhancement.process(match)
        #                valid_matches.append(match)
        #            except DropMatchException as e:
        #                pass
        #            except EAException as e:
        #                self.handle_error("Error running match enhancement: %s" % (e), {'rule': rule['name']})
        #        matches = valid_matches
        #        if not matches:
        #            return None

        # Don't send real alerts in debug mode
        if self.debug:
            alerter = DebugAlerter(rule)
            alerter.alert(matches)
            return None

        # Run the alerts
        alert_sent = False
        alert_exception = None
        # Alert.pipeline is a single object shared between every alerter
        # This allows alerters to pass objects and data between themselves
        alert_pipeline = {"alert_time": alert_time}
        for alert in rule.get("actions"):
            alert.pipeline = alert_pipeline
            try:
                alert.alert(matches)
            except EAException as e:
                self.handle_error('Error while running alert %s: %s' % (alert.get_info()['type'], e), {'rule': rule['name']})
                alert_exception = str(e)
            else:
                self.alerts_sent += 1
                alert_sent = True

        # Write the alert(s) to ES
        #agg_id = None
        #for match in matches:
        #    alert_body = self.get_alert_body(match, rule, alert_sent, alert_time, alert_exception)
            # Set all matches to aggregate together
        #    if agg_id:
        #        alert_body['aggregate_id'] = agg_id
        #    res = self.writeback('elastalert', alert_body)
        #    if res and not agg_id:
        #        agg_id = res['_id']

    def get_alert_body(self, match, rule, alert_sent, alert_time, alert_exception=None):
        body = {'match_body': match}
        body['rule_name'] = rule['name']
        # TODO record info about multiple alerts
        body['alert_info'] = rule['alert'][0].get_info()
        body['alert_sent'] = alert_sent
        body['alert_time'] = alert_time

        # If the alert failed to send, record the exception
        if not alert_sent:
            body['alert_exception'] = alert_exception
        return body

    def writeback(self, doc_type, body):
        # Convert any datetime objects to timestamps
        for key in body.keys():
            if isinstance(body[key], datetime.datetime):
                body[key] = dt_to_ts(body[key])
        if self.debug:
            elastalert_logger.info("Skipping writing to ES: %s" % (body))
            return None

        if '@timestamp' not in body:
            body['@timestamp'] = dt_to_ts(ts_now())
        if self.writeback_es:
            try:
                res = self.writeback_es.create(index=self.writeback_index,
                                               doc_type=doc_type, body=body)
                return res
            except ElasticsearchException as e:
                logging.exception("Error writing alert info to elasticsearch: %s" % (e))
                self.writeback_es = None

    def find_recent_pending_alerts(self, time_limit):
        """ Queries writeback_es to find alerts that did not send
        and are newer than time_limit """

        # XXX only fetches 1000 results. If limit is reached, next loop will catch them
        # unless there is constantly more than 1000 alerts to send.

        # Fetch recent, unsent alerts that aren't part of an aggregate, earlier alerts first.
        query = {'query': {'query_string': {'query': '!_exists_:aggregate_id AND alert_sent:false'}},
                 'filter': {'range': {'alert_time': {'from': dt_to_ts(ts_now() - time_limit),
                                                     'to': dt_to_ts(ts_now())}}},
                 'sort': {'alert_time': {'order': 'asc'}}}
        if self.writeback_es:
            try:
                res = self.writeback_es.search(index=self.writeback_index,
                                               doc_type='elastalert',
                                               body=query,
                                               size=1000)
                if res['hits']['hits']:
                    return res['hits']['hits']
            except:  # TODO: Give this a more relevant exception, try:except: is evil.
                pass
        return []

    def send_pending_alerts(self):
        pending_alerts = self.find_recent_pending_alerts(self.alert_time_limit)
        for alert in pending_alerts:
            _id = alert['_id']
            alert = alert['_source']
            try:
                rule_name = alert.pop('rule_name')
                alert_time = alert.pop('alert_time')
                match_body = alert.pop('match_body')
            except KeyError:
                # Malformed alert, drop it
                continue

            # Find original rule
            for rule in self.rules:
                if rule['name'] == rule_name:
                    break
            else:
                # Original rule is missing, keep alert for later if rule reappears
                continue

            # Set current_es for top_count_keys query
            rule_es_conn_config = self.build_es_conn_config(rule)
            self.current_es = self.new_elasticsearch(rule_es_conn_config)
            self.current_es_addr = (rule['es_host'], rule['es_port'])

            # Send the alert unless it's a future alert
            if ts_now() > ts_to_dt(alert_time):
                aggregated_matches = self.get_aggregated_matches(_id)
                if aggregated_matches:
                    matches = [match_body] + [agg_match['match_body'] for agg_match in aggregated_matches]
                    self.alert(matches, rule, alert_time=alert_time)
                    if rule['current_aggregate_id'] == _id:
                        rule['current_aggregate_id'] = None
                else:
                    self.alert([match_body], rule, alert_time=alert_time)

                # Delete it from the index
                try:
                    self.writeback_es.delete(index=self.writeback_index,
                                             doc_type='elastalert',
                                             id=_id)
                except:  # TODO: Give this a more relevant exception, try:except: is evil.
                    self.handle_error("Failed to delete alert %s at %s" % (_id, alert_time))

        # Send in memory aggregated alerts
        for rule in self.rules:
            if rule['agg_matches']:
                if ts_now() > rule['aggregate_alert_time']:
                    self.alert(rule['agg_matches'], rule)
                    rule['agg_matches'] = []

    def get_aggregated_matches(self, _id):
        """ Removes and returns all matches from writeback_es that have aggregate_id == _id """

        # XXX if there are more than self.max_aggregation matches, you have big alerts and we will leave entries in ES.
        query = {'query': {'query_string': {'query': 'aggregate_id:%s' % (_id)}}}
        matches = []
        if self.writeback_es:
            try:
                res = self.writeback_es.search(index=self.writeback_index,
                                               doc_type='elastalert',
                                               body=query,
                                               size=self.max_aggregation)
                for match in res['hits']['hits']:
                    matches.append(match['_source'])
                    self.writeback_es.delete(index=self.writeback_index,
                                             doc_type='elastalert',
                                             id=match['_id'])
            except (KeyError, ElasticsearchException) as e:
                self.handle_error("Error fetching aggregated matches: %s" % (e), {'id': _id})
        return matches

    def find_pending_aggregate_alert(self, rule):
        query = {'filter': {'bool': {'must': [{'term': {'rule_name': rule['name']}},
                                              {'range': {'alert_time': {'gt': ts_now()}}},
                                              {'not': {'exists': {'field': 'aggregate_id'}}},
                                              {'term': {'alert_sent': 'false'}}]}},
                 'sort': {'alert_time': {'order': 'desc'}}}
        if not self.writeback_es:
            self.writeback_es = self.new_elasticsearch(self.es_conn_config)
        try:
            res = self.writeback_es.search(index=self.writeback_index,
                                           doc_type='elastalert',
                                           body=query,
                                           size=1)
            if len(res['hits']['hits']) == 0:
                return None
        except (KeyError, ElasticsearchException) as e:
            self.handle_error("Error searching for pending aggregated matches: %s" % (e), {'rule_name': rule['name']})
            return None

        return res['hits']['hits'][0]

    def add_aggregated_alert(self, match, rule):
        """ Save a match as a pending aggregate alert to elasticsearch. """
        if (not rule['current_aggregate_id'] or
                ('aggregate_alert_time' in rule and rule['aggregate_alert_time'] < ts_to_dt(match[rule['timestamp_field']]))):

            # Elastalert may have restarted while pending alerts exist
            pending_alert = self.find_pending_aggregate_alert(rule)
            if pending_alert:
                alert_time = rule['aggregate_alert_time'] = ts_to_dt(pending_alert['_source']['alert_time'])
                agg_id = rule['current_aggregate_id'] = pending_alert['_id']
                elastalert_logger.info('Adding alert for %s to aggregation(id: %s), next alert at %s' % (rule['name'], agg_id, alert_time))
            else:
                # First match, set alert_time
                match_time = ts_to_dt(match[rule['timestamp_field']])
                alert_time = ''
                if isinstance(rule['aggregation'], dict) and rule['aggregation'].get('schedule'):
                    croniter._datetime_to_timestamp = cronite_datetime_to_timestamp  # For Python 2.6 compatibility
                    try:
                        iter = croniter(rule['aggregation']['schedule'], ts_now())
                        alert_time = unix_to_dt(iter.get_next())
                    except Exception as e:
                        self.handle_error("Error parsing aggregate send time Cron format %s" % (e), rule['aggregation']['schedule'])
                else:
                    alert_time = match_time + rule['aggregation']

                rule['aggregate_alert_time'] = alert_time
                agg_id = None
                elastalert_logger.info('New aggregation for %s. next alert at %s.' % (rule['name'], alert_time))
        else:
            # Already pending aggregation, use existing alert_time
            alert_time = rule['aggregate_alert_time']
            agg_id = rule['current_aggregate_id']
            elastalert_logger.info('Adding alert for %s to aggregation(id: %s), next alert at %s' % (rule['name'], agg_id, alert_time))

        alert_body = self.get_alert_body(match, rule, False, alert_time)
        if agg_id:
            alert_body['aggregate_id'] = agg_id
        res = self.writeback('elastalert', alert_body)

        # If new aggregation, save _id
        if res and not agg_id:
            rule['current_aggregate_id'] = res['_id']

        # Couldn't write the match to ES, save it in memory for now
        if not res:
            rule['agg_matches'].append(match)

        return res

    def handle_error(self, message, data=None):
        ''' Logs message at error level and writes message, data and traceback to Elasticsearch. '''
        if not self.writeback_es:
            self.writeback_es = self.new_elasticsearch(self.es_conn_config)

        logging.error(message)
        body = {'message': message}
        tb = traceback.format_exc()
        body['traceback'] = tb.strip().split('\n')
        if data:
            body['data'] = data
        self.writeback('elastalert_error', body)

    def handle_uncaught_exception(self, exception, rule):
        """ Disables a rule and sends a notification. """
        logging.error(traceback.format_exc())
        self.handle_error('Uncaught exception running rule %s: %s' % (rule['name'], exception), {'rule': rule['name']})
        if self.disable_rules_on_error:
            self.rules = [running_rule for running_rule in self.rules if running_rule['name'] != rule['name']]
            self.disabled_rules.append(rule)
            elastalert_logger.info('Rule %s disabled', rule['name'])
        if self.notify_email:
            self.send_notification_email(exception=exception, rule=rule)


    def run_rule(self, rule):
        """ Run a rule including querying and alerting on results.

        :param rule: The rule configuration.
        :return: The number of matches that the rule produced.
        """

        elastalert_logger.info('Start to run rule ........')
        # Run the rule. If querying over a large time period, split it up into segments
        self.num_hits = 0
        self.current_es = self.new_elasticsearch(self.global_config)
        
        self.run_query(rule)

        # Process any new matches
        num_matches = len(rule['type'].matches)
        while rule['type'].matches:
            match = rule['type'].matches.pop(0)

            #if self.is_silenced(rule['name'] + key) or self.is_silenced(rule['name']):
            #    elastalert_logger.info('Ignoring match for silenced rule %s%s' % (rule['name'], key))
            #    continue

            if rule.get('realert'):
                next_alert, exponent = self.next_alert_time(rule, rule['name'] + key, ts_now())
                self.set_realert(rule['name'] + key, next_alert, exponent)

            # If no aggregation, alert immediately
            #if not rule['aggregation']:
            #    self.alert([match], rule)
            #    continue
            self.alert([match], rule)

            # Add it as an aggregated match
            #self.add_aggregated_alert(match, rule)

        # Mark this endtime for next run's start
        #rule['previous_endtime'] = endtime

        #time_taken = time.time() - run_start
        

        return num_matches

    
    def stop(self):
        """ Stop an elasticwatcher runner that's been started """

        self.running = False


    def start(self):
        
        self.running = True
        elastalert_logger.info("Starting up")

        self.scheduler = schedule.Manager()
        for rule in self.rules:
            rule_interval = intervalInSecond(rule.get("trigger").get("schedule").get("interval"))
            self.scheduler.add_operation(self.run_rule, rule_interval, [rule])
            #rule_timer = threading.Timer(rule_interval, self.run_rule, [rule])
            #self.rule_jobs.append(rule_timer)
            #rule_timer.start()


def handle_signal(signal, frame):
    elastalert_logger.info('SIGINT received, stopping ElastWatcher...')
    # use os._exit to exit immediately and avoid someone catching SystemExit
    os._exit(0)


def main(args=None):
    signal.signal(signal.SIGINT, handle_signal)
    if not args:
        args = sys.argv[1:]
    client = ElastWatcher(args)
    client.start()
    while True:
        time.sleep(.1)

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
