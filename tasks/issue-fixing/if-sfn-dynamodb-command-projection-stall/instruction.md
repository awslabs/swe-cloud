The command service accepts new versions, but some customer projections stop
advancing while their executions remain RUNNING. Retrying publication has made
the backlog longer. Please repair the processing service under `/app` so new
commands drain without manual callback completion.

Keep the existing commit order and single application of downstream effects,
including when delivery starts more than one execution for a version. A failed
predecessor must not let later effects through. We are fixing future processing;
do not replay the historical backlog.

`README.md` describes the deployment and how to run it against the local AWS
endpoint. Keep the `solution wait-worker` and `solution finish-worker` process
interfaces. The publisher, state machine and projection sink are deployment
components, not part of this repair. Workers run in separate working directories;
all durable coordination must use the services, not local files.
