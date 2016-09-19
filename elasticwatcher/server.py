import bottle
from bottle import route, run
from datetime import datetime
import json
from bottle import request, response
from bottle import post, get, put, delete
from elasticsearch import Elasticsearch
from watcher import ElastWatcher
import signal
import sys
import time
import threading


elastic = Elasticsearch(host="10.29.97.129",port=9200)

@put('/watches')
def put_watchers_handler():
    '''Handles watcher creation'''
    try:
    	print request.body
        # parse input data
        try:
            data = request.json
            print "data:%s", data
        except:
            raise ValueError

        if data is None:
            raise ValueError

        res = elastic.index(index="watches", doc_type='watch', body=data)
        print(res['created'])

    except ValueError:
        # if bad request data, return 400 Bad Request
        response.status = 400
        return
    
    except KeyError:
        # if name already exists, return 409 Conflict
        response.status = 409
        return
    
    # return 200 Success
    response.headers['Content-Type'] = 'application/json'
    return json.dumps({'result': "created successfully"})

@get('/watches')
def get_watches_handler():
    '''Handles watches listing'''
    response.headers['Content-Type'] = 'application/json'
    response.headers['Cache-Control'] = 'no-cache'
    query_results = elastic.search(index="watches", doc_type='watch', body={"query": {"match_all": {}}})
    print("Got %d Hits:" % query_results['hits']['total'])
    watchers = []
    for hit in query_results['hits']['hits']:
    	watcher = {}
    	watcher['watch'] = hit["_source"]
    	watcher['id'] = hit["_id"]
    	watchers.append(watcher)
    
    return json.dumps(watchers)

@get('/watches/<watch_name>')
def get_watch_handler(watch_name):
	'''Handles show specific watch'''
	response.headers['Content-Type'] = 'application/json'
	response.headers['Cache-Control'] = 'no-cache'
	query = {"query": {
        "match": {"name": watch_name}
      }
    }
	query_results = elastic.search(index="watches", doc_type='watch', body=query)
	print("Got %d Hits:" % query_results['hits']['total'])
	watchers = []
	for hit in query_results['hits']['hits']:
		watcher = {}
		watcher['watch'] = hit["_source"]
		watcher['id'] = hit["_id"]
		watchers.append(watcher)
	return json.dumps(watchers)

app = application = bottle.default_app()

def watcher_service(args=None):
    print "test, leon.............."
    if not args:
        args = sys.argv[1:]
    client = ElastWatcher(args)
    client.start()
    while True:
        time.sleep(.1)

def handle_signal(signal, frame):
    elastalert_logger.info('SIGINT received, stopping ElastWatcher...')
    # use os._exit to exit immediately and avoid someone catching SystemExit
    os._exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_signal)
    t = threading.Thread(name='watcher_daemon', target=watcher_service)
    t.start()

    bottle.run(host = 'localhost', port = 8888, debug=True)
