# -*- coding: utf-8 -*-
import os

from setuptools import find_packages
from setuptools import setup


base_dir = os.path.dirname(__file__)
setup(
    name='elasticwatcher',
    version='0.0.1',
    description='Runs custom filters on Elasticsearch and alerts on matches',
    author='Quentin Long',
    author_email='zhanyingle@gmail.com',
    setup_requires='setuptools',
    license='Copyright 2016',
    entry_points={
        'console_scripts': ['elasticwatcher=elasticwatcher.watcher:main']},
    packages=find_packages(),
    install_requires=[
        'argparse',
        'elasticsearch',
        'jira==0.32',  # jira.exceptions is missing from later versions
        'jsonschema',
        'mock',
        'python-dateutil',
        'PyStaticConfiguration',
        'pyyaml',
        'simplejson',
        'boto',
        'botocore',
        'blist',
        'croniter',
        'configparser',
        'aws-requests-auth'
    ]
)
