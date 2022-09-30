==============================
Controlling Run Engine Manager
==============================

Bluesky HTTP Server provides REST API that allow to access all low-level API
exposed by Run Engine (RE) Manager. The server forwards payloads of most REST API
requests to RE Manager over 0MQ and forwards the returned results back to the
client. Some of the parameters used to monitor RE Manager and RE Worker are cached
by HTTP Server and returned to clients without sending requests to RE Manager
each time. Caching removes the load on RE Manager, especially if multiple clients
are monitoring the experiment simultaneously.

Users are expected to control the server using Python scripts based on
`Bluesky Queue Server API <https://blueskyproject.io/bluesky-queueserver-api/>`_,
GUI programs or Web applications. This manual demonstrates how to send the API requests
from command line using `httpie <https://httpie.io/>`_. This approach is not practical,
but may be useful for testing the server and understanding the API.

.. note::

    Unless the server is configured to allow public access to all API, all requests
    must include a valid access token or an API key. See the instructions on how
    to send tokens and API keys with API requests in section
    :ref:`passing_tokens_and_api_keys_with_api_requests`

Starting Queue Server Stack for the Demo
----------------------------------------

This section contains instructions on how to start RE Manager and HTTP Server to explore the API
in demo mode. See :ref:`starting_http_server` for more detailed information.

Start RE Manager and enable publishing of console output::

  $ start-re-manager --zmq-publish-console ON

Start HTTP Server in single-user mode. In this example the single-user API key is ``mykey``, but 
it may be any alphanumeric string::

  $ QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY=mykey uvicorn --host localhost --port 60610 bluesky_httpserver.server:app 

Now the server API can be accessed by passing the API key with API requests. For example, the status of
RE Manager can be loaded using ::

  $ http GET http://localhost:60610/api/status 'Authorization: ApiKey mykey'

.. note::

  In rare cases RE Manager or HTTP Server may crash and leave some sockets open. This was observed
  when running development versions of RE Manager that contain bugs. The remaining open sockets
  may prevent RE Manager or HTTP Server from restarting. The sockets could be closed by
  running ::
  
    $ netstat -ltnp

  and finding PIDs of the offending processes. The default ports used by RE Manager are
  60615 and 60625 and port used by the HTTP Server is 60610. Kill the offending processes::

    $ kill -9 <pid>


Guide to RE Manager API
-----------------------

The most basic request is 'ping' intended to fetch some response from RE Manager::

  http GET http://localhost:60610/api
  http GET http://localhost:60610/api/ping


Currently 'ping' request returns the status of RE Manager, but the returned data may change. The recommended
way to fetch status of RE Manager is to use 'status' request::

  http GET http://localhost:60610/api/status

Before plans could be executed, the RE Worker environment must be opened. Opening RE Worker environment
involves loading beamline profile collection and instantiation of Run Engine and may take a few minutes.
The package comes with simulated profile collection that includes simulated Ophyd devices and built-in
Bluesky plans and loads almost instantly. An open RE Worker environment may be closed or destroyed.
Orderly closing of the environment is a safe operation, which is possible only when RE Worker
(and RE Manager) is in idle state, i.e. no plans are currently running or paused. Destroying
the environment is potentially dangerous, since it involves killing of RE Process that could potentially
be running plans, and supposed to be used for destroying unresponsive environment in case of RE failure.
Note that any operations on the queue (such as adding or removing plans) can be performed before
the environment is opened.

Open the new RE environment::

  http POST http://localhost:60610/api/environment/open

Close RE environment::

  http POST http://localhost:60610/api/environment/close

Destroy RE environment::

  http POST http://localhost:60610/api/environment/destroy

Get the lists (JSON) of allowed plans and devices::

  http POST http://localhost:60610/api/plans/allowed
  http POST http://localhost:60610/api/devices/allowed

The list of allowed plans and devices is generated based on the list of existing plans and devices
('existing_plans_and_devices.yaml' by default) and user group permissions ('user_group_permissions.yaml'
by default). The files with permission data are loaded at RE Manager startup. If any of the files
are changed while RE Manager is running (e.g. a new plan was added to the profile collection and
the new 'existing_plans_and_devices.yaml' file was generated) and restarting RE Manager is not
desirable, the data can be reloaded by sending 'permissions_reload' request::

  http GET http://localhost:60610/api/permissions/reload

Before plans could be executed they should be placed in the **plan queue**. The plan queue contains
**items**. The items are **plans** that could be executed by Run Engine or **instructions** that
can modify the state of the queue or RE Manager. Currently only one instruction ('queue_stop' - stops
execution of the queue) is supported.

Push a new plan to the back of the queue::

  http POST http://localhost:60610/api/queue/item/add item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'
  http POST http://localhost:60610/api/queue/item/add item:='{"name":"scan", "args":[["det1", "det2"], "motor", -1, 1, 10], "item_type": "plan"}'
  http POST http://localhost:60610/api/queue/item/add item:='{"name":"count", "args":[["det1", "det2"]], "kwargs":{"num":10, "delay":1}, "item_type": "plan"}'

It takes 10 second to execute the third plan in the group above, so it is may be the most convenient for testing
pausing/resuming/stopping of experimental plans.

API for queue operations is designed to work identically with items of all types. For example, a 'queue_stop`
instruction can be added to the queue `queue_item_add` API::

  http POST http://localhost:60610/api/queue/item/add item:='{"name":"queue_stop", "item_type": "instruction"}'

An item can be added at any position of the queue. Push a plan to the front or the back of the queue::

  http POST http://localhost:60610/api/queue/item/add pos:='"front"' item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'
  http POST http://localhost:60610/api/queue/item/add pos:='"back"' item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'
  http POST http://localhost:60610/api/queue/item/add pos:=2 item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

The following command will insert an item in place of the last item in the queue; the last item remains
the last item in the queue::

  http POST http://localhost:60610/api/queue/item/add pos:=-1 item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

An item can be inserted before or after an existing item with given Item UID.
Insert the plan before an existing item with <uid>::

  http POST http://localhost:60610/api/queue/item/add before_uid:='<uid>' item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

Insert the plan after an existing item with <uid>::

  http POST http://localhost:60610/api/queue/item/add after_uid:='<uid>' item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

If the queue has 5 items (0..4), then the following command pushes the new plan to the back of the queue::

  http POST http://localhost:60610/api/queue/item/add pos:=5 item:='{"name":"count", "args":[["det1", "det2"]], "item_type": "plan"}'

The 'queue_item_add' request will accept any index value. If the index is out of range, then the item will
be pushed to the front or the back of the queue. If the queue is currently running, then it is recommended
to access elements using negative indices (counted from the back of the queue).

The names of the plans and devices are strings. The strings are converted to references to Bluesky plans and
Ophyd devices in the worker process. The simulated beamline profile collection includes all simulated
Ophyd devices and built-in Bluesky plans.

A batch of plans may be submitted to the queue by sending a single request. Every plan in the batch
is validated and the plans are added to the queue only if all plans pass validation. Otherwise the
batch is rejected. The following request adds two plans to the queue::

  http POST http://localhost:60610/api/queue/item/add/batch items:='[{"name":"count", "args":[["det1"]], "item_type": "plan"}, {"name":"count", "args":[["det2"]], "item_type": "plan"}]'

Alternatively the queue may be populated by uploading the list of plans with parameters in the form of
a spreadsheet to HTTP server. Note that this is an experimental feature, which could be modified at any
time until API is settled. The format of the spreadsheet will be specific to each beamline
using the server. Beamline-specific code will be distributed in a separate package from the core HTTP
server code. Currently, to upload spreadsheet located at `../sample_excel.xlsx` (could be arbitrary path)
run the following command::

  http --form POST http://localhost:60610/api/queue/upload/spreadsheet spreadsheet@../sample_excel.xlsx

Queue Server API allow to execute a single item (plan or instruction) submitted with the API call. Execution
of an item starts immediately if possible (RE Manager is idle and RE Worker environment exists), otherwise
API call fails and the item is not added to the queue. The following commands start execution of a single plan::

  http POST http://localhost:60610/api/queue/item/execute item:='{"name":"count", "args":[["det1", "det2"]], "kwargs":{"num":10, "delay":1}, "item_type": "plan"}'

Queue can be edited at any time. Changes to the running queue become effective the moment they are
performed. As the currently running plan is finished, the new plan is popped from the top of the queue.

The contents of the queue may be fetched at any time::

  http GET http://localhost:60610/api/queue/get

The last item can be removed (popped) from the back of the queue::

  echo '{}' | http POST http://localhost:60610/api/queue/item/remove
  http POST http://localhost:60610/api/queue/item/remove pos:='"back"'

The position of the removed item may be specified similarly to `queue_item_add` request with the difference
that the position index must point to the existing element, otherwise the request fails (returns 'success==False').
The following examples remove the plan from the front of the queue and the element previous to last::

  http POST http://localhost:60610/api/queue/item/remove pos:='"front"'
  http POST http://localhost:60610/api/queue/item/remove pos:=-2

The items can also be addressed by UID. Remove the item with <uid>::

  http POST http://localhost:60610/api/queue/item/remove uid:='<uid>'

Items can be read from the queue without changing it. `queue_item_get` requests are formatted identically to
`queue_item_remove` requests::

  echo '{}' | http GET http://localhost:60610/api/queue/item/get
  http GET http://localhost:60610/api/queue/item/get pos:='"back"'
  http GET http://localhost:60610/api/queue/item/get pos:='"front"'
  http GET http://localhost:60610/api/queue/item/get pos:=-2
  http GET http://localhost:60610/api/queue/item/get uid:='<uid>'

Items can be moved within the queue. Items can be addressed by position or UID. If positional addressing
is used then items are moved from 'source' position to 'destination' position.
If items are addressed by UID, then the item with <uid_source> is inserted before or after
the item with <uid_dest>::

  http POST http://localhost:60610/api/queue/item/move pos:=3 pos_dest:=5
  http POST http://localhost:60610/api/queue/item/move uid:='<uid_source>' before_uid:='<uid_dest>'
  http POST http://localhost:60610/api/queue/item/move uid:='<uid_source>' after_uid:='<uid_dest>'

Addressing by position and UID can be mixed. The following instruction will move queue item #3
to the position following an item with <uid_dest>::

  http POST http://localhost:60610/api/queue/item/move pos:=3 after_uid:='<uid_dest>'

The following instruction moves item with <uid_source> to the front of the queue::

  http POST http://localhost:60610/api/queue/item/move uid:='<uid_source>' pos_dest:='"front"'

The parameters of queue items may be updated or replaced. When the item is replaced, it is assigned a new
item UID, while if the item is updated, item UID remains the same. The API implementing those
operations does not distinguish plans and instructions, i.e. an instruction may be updated/replaced
by a plan or a plan by an instruction. The operation is performed by REST API */queue/item/update*.
Item parameter *'item_uid'* must be set to the UID of the item to be updated. Additional
API parameter 'replace' determines if the item is updated or replaced. If the parameter
is skipped or set *false*, the item is updated. If the parameter is set *true*,
the item is replaced (i.e. new item UID is generated)::

  http POST http://localhost:60610/api/queue/item/update item:='{"item_uid":"<existing-uid>", "name":"count", "args":[["det1", "det2"]], "item_type":"plan"}'
  http POST http://localhost:60610/api/queue/item/update item:='{"item_uid":"<existing-uid>", "name":"queue_stop", "item_type":"instruction"}'
  http POST http://localhost:60610/api/queue/item/update replace:=true item:='{"item_uid":"<existing-uid>", "name":"count", "args":[["det1", "det2"]], "item_type":"plan"}'
  http POST http://localhost:60610/api/queue/item/update replace:=true item:='{"item_uid":"<existing-uid>", "name":"queue_stop", "item_type":"instruction"}'

Remove all entries from the plan queue::

  http POST http://localhost:60610/api/queue/clear

The plan queue can operate in LOOP mode, which is disabled by default. To enable or disable the LOOP mode
the following commands::

  http POST http://localhost:60610/api/queue/mode/set mode:='{"loop": true}'
  http POST http://localhost:60610/api/queue/mode/set mode:='{"loop": false}'

Start execution of the plan queue. The environment MUST be opened before queue could be started::

  http POST http://localhost:60610/api/queue/start

Request to execute an empty queue is a valid operation that does nothing.

As the queue is running, the list of active runs (runs generated by the running plan may be obtained
at any time). The set of active runs consists of two subsets: open runs and closed runs. For
simple single-run plans the list will contain only one item. The list can be loaded using CLI
commands and HTTP API::

  http GET http://localhost:60610/api/re/runs/active  # Get the list of active runs
  http GET http://localhost:60610/api/re/runs/open    # Get the list of open runs
  http GET http://localhost:60610/api/re/runs/closed  # Get the list of closed runs

The queue can be stopped at any time. Stopping the queue is a safe operation. When the stopping
sequence is initiated, the currently running plan is finished and the next plan is not be started.
The stopping sequence can be cancelled if it was activated by mistake or decision was changed::

  http POST http://localhost:60610/api/queue/stop
  http POST http://localhost:60610/api/queue/stop/cancel

While a plan in a queue is executed, operation Run Engine can be paused. In the unlikely event
if the request to pause is received while RunEngine is transitioning between two plans, the request
may be rejected by the RE Worker. In this case it needs to be repeated. If Run Engine is in the paused
state, plan execution can be resumed, aborted, stopped or halted. If the plan is aborted, stopped
or halted, it is not removed from the plan queue (it remains the first in the queue) and execution
of the queue is stopped. Execution of the queue may be started again if needed.

Running plan can be paused immediately (returns to the last checkpoint in the plan) or at the next
checkpoint (deferred pause)::

  http POST http://localhost:60610/api/re/pause option="deferred"
  http POST http://localhost:60610/api/re/pause option="immediate"

Resuming, aborting, stopping or halting of currently executed plan::

  http POST http://localhost:60610/api/re/resume
  http POST http://localhost:60610/api/re/stop
  http POST http://localhost:60610/api/re/abort
  http POST http://localhost:60610/api/re_halt

There is minimal user protection features implemented that will prevent execution of
the commands that are not supported in current state of the server. Error messages are printed
in the terminal that is running the server along with output of Run Engine.

Data on executed plans, including stopped plans, is recorded in the history. History can
be downloaded at any time::

  http GET http://localhost:60610/api/history/get

History is not intended for long-term storage. It can be cleared at any time::

  http POST http://localhost:60610/api/history/clear

Stop RE Manager (exit RE Manager application). There are two options: safe request that is rejected
when the queue is running or a plan is paused::

  echo '{}' | http POST http://localhost:60610/api/manager/stop
  http POST http://localhost:60610/api/manager/stop option="safe_on"

Manager can be also stopped at any time using unsafe stop, which causes current RE Worker to be
destroyed even if a plan is running::

  http POST http://localhost:60610/api/manager/stop option="safe_off"

The 'test_manager_kill' request is designed specifically for testing ability of RE Watchdog
to restart malfunctioning RE Manager process. This command stops event loop of RE Manager process
and causes RE Watchdog to restart the process (currently after 5 seconds). RE Manager
process is expected to fully recover its state, so that the restart does not affect
running or paused plans or the state of the queue. Another potential use of the request
is to test handling of communication timeouts, since RE Manager does not respond to the request::

  http POST http://localhost:60610/api/test/manager/kill


Additional API
--------------

API that are implemented, but not yet included in the guide:

- ``/api/re/runs`` - access to ``re_runs``, combines ``/api/re/runs/active``, ``/api/re/runs/open``, ``/api/re/runs/closed``
- ``/api/plans/existing`` - access to ``plans_existing`` API
- ``/api/devices/existing`` - access to ``devices_existing`` API
- ``/api/permissions/get`` - access to ``permissions_get`` API
- ``/api/permissions/set`` - access to ``permissions_set`` API
- ``/api/script/upload`` - access to ``script_upload`` API
- ``/api/function/execute`` - access to ``function_execute`` API
- ``/api/task/status`` - access to ``task_status`` API
- ``/api/task/result`` - access to ``task_result`` API
- ``/api/lock`` - lock RE Manager
- ``/api/lock/info`` - load RE Manager lock status, optionally verify a lock key
- ``/api/unlock`` - unlock RE Manager

- ``/test/server/sleep`` - causes server to reply after the specified delay.

Streaming Console Output of RE Manager
--------------------------------------

HTTP server provides streaming API ``stream_console_output`` that allows web applications to receive,
process and display captured console output of RE manager. To test operation of the streaming API,
enable publishing of console output by RE Manager::

  start-re-manager --zmq-publish-console ON

start HTTP Server, start Web Browser and type the following address::

  http://localhost:60610/stream_console_output

Then open a separate terminal and send a few requests to RE Manager, e.g. ::

  http POST http://localhost:60610/api/environment/open
  http POST http://localhost:60610/api/environment/close


JSON representation of console output message (timestamp and text message) will be displayed
in the browser, e.g. ::

{"time": 1629816304.5475085, "msg": "INFO:bluesky_queueserver.manager.manager:Opening the new RE environment ...\n"}

Client application is responsible for processing JSON messages and displaying formatted output to users.

HTTP Server is not performing caching of the console output, so streamed data contain only messages
received after the web client connects to the server.

If RE Manager is configured to publish console address to 0MQ socket with port number different from
default or HTTP server is running on a separate workstation/server, the address of 0MQ socket
can be specified by setting the environment variable ``QSERVER_ZMQ_INFO_ADDRESS``, e.g. ::

  export QSERVER_ZMQ_INFO_ADDRESS='tcp://localhost:60625'


Console Output of RE Manager
----------------------------
In some cases, using streaming console output is inconvenient or difficult. The server
provides endpoint ``/console_output`` returns the last ``nlines`` of the console output
represented as a text string. The parameter ``nlines`` is optional with the default value of 200.
The maximum number of returned lines is limited (currently to 2000 lines). ::

  http GET http://localhost:60610/api/console_output
  http GET http://localhost:60610/api/console_output lines=500

Continuously reloading the text buffer from the server even if it contains no new data is
inefficient. The ``/console_output/uid`` API returns UID of the console output buffer.
Polling the UID and reloading the text buffer only when UID has changed improves efficiency
and reduces load on the server. ::

  http GET http://localhost:60610/api/console_output/uid

If the client application can reconstruct the text from a stream of messages, the
``/console_output_update`` API can be used to load messages accumulated after
the message with a given UID passed as a parameter. By polling the API using
UID of the last downloaded message, the application can load the new messages
and generate the text output locally without repeatedly reloading the text
buffer with each buffer update as in the case of ``/console_output`` API. ::

  http GET http://localhost:60610/api/console_output_update last_msg_uid=<last-message-uid>
