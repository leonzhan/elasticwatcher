#!/usr/bin/env ruby

require 'json'
require 'optparse'
require 'pp'

$options = {}

OptionParser.new do |opts|
  $options[:query_result] = {}
  opts.on("-q", "--query_result <string>", String, "query result data with json string format") do |v|
    $options[:query_result] = v
  end
  # No argument, shows at tail.  This will print an options summary.
  opts.on_tail("--help", "Show this message") do
    puts opts
    exit
  end
end.parse!

matched = false
    
begin 
  query_result = JSON.parse($options[:query_result])
  if (query_result['hits']['total'] < 1); 
    matched = false
  end 
  rows = query_result['hits']['hits'] 
  if (rows[0]['_source']['cluster_state']['status'] == 'green');
    matched = true
  end
  agg_buckets=query_result['aggregations']['minutes']['buckets'] 

  last60Seconds = agg_buckets[-12..-1] 
  for it in last60Seconds
    if it['status']['buckets'] &&  it['status']['buckets'].size > 0
      key = it['status']['buckets'][0]['key'] 
      it['status']['buckets'][0]['key'] == 'red' if key
    else
      next
    end
  end
  
rescue => e
  puts e.message
end

print matched
