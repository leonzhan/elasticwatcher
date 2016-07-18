#!/usr/bin/env ruby

require 'json'
require 'optparse'
require 'pp'

def check_condition(query_result, script)
    size=yield(query_result, script)
end

$options = {}

OptionParser.new do |opts|
  $options[:query_result] = {}
  opts.on("-q", "--query_result <string>", String, "query result data with json string format") do |v|
    $options[:query_result] = v
  end
  $options[:script] = ''
  opts.on("-s", "--script <string>", String, "ruby script to dertermine whether alert match") do |v|
    $options[:script] = v
  end  
  # No argument, shows at tail.  This will print an options summary.
  opts.on_tail("--help", "Show this message") do
    puts opts
    exit
  end
end.parse!

begin
  #lambda (ctx, script): |ctx, script| eval script
  check = -> (ctx, script) { eval script }
  check.call(JSON.parse($options[:query_result]), $options[:script])
  #check_condition(JSON.parse($options[:query_result]), $options[:script]) { |ctx, script| 
  #	begin
  #      print "leon, 1.." 
  #	  eval script
  #	rescue => e
  #    print "leon, 2.." 
  #	  PP.pp e.backtrace
  #	end
  #}

rescue => e
  print "leon, 3.." 
  PP.pp e.backtrace
ensure
  
end
