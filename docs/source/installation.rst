============
Installation
============


The Queue Server is using Redis to store and manage the queue. If you are planing to run Run Engine Manager
on the same machine as the HTTP Server, then install Redis as described in `the Queue Server documentation 
<https://blueskyproject.io/bluesky-queueserver/installation.html>`_.

Installing HTTP Server from conda-forge (environment name is ``qserver-env``, use any convenient name)::

    $ conda create -n qserver-env python=3.9 -c conda-forge
    $ conda activate qserver-env
    $ conda install bluesky-httpserver -c conda-forge

Installing HTTP Server from PyPI::

    $ pip install bluesky-httpserver

Installing HTTP Server from source (for development)::

    $ git clone https://github.com/bluesky/bluesky-httpserver
    $ cd bluesky-httpserver
    $ pip install -e .

