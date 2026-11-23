# otlp-emf-forwarder: CloudWatch Embedded Metric Format

This is a short summary of the public CloudWatch Embedded Metric Format (EMF)
specification. It describes the documents that `otlp-emf-forwarder` writes as
CloudWatch Logs events. How OTLP data maps onto these documents is the
behavior under study and is not described here.

An EMF document is a JSON object (one per log event) with:

* A root member `_aws`, the metadata object:
  * `Timestamp`: the metric time as an integer number of milliseconds since
    the Unix epoch.
  * `CloudWatchMetrics`: an array of metric directives. Each directive has:
    * `Namespace`: the CloudWatch namespace string.
    * `Dimensions`: an array of dimension sets. Each set is an array of
      member names in the root object whose string values are the dimension
      values. An empty set (`[]`) declares metrics without dimensions.
    * `Metrics`: an array of metric definitions, each with `Name` (a
      member name in the root object holding the value) and optional `Unit`
      (a CloudWatch unit name such as `Count`, `Seconds`, `Milliseconds`,
      `Bytes`) and `StorageResolution` (`60` for standard resolution, `1`
      for high resolution).
* Target members at the root, referenced by the directive:
  * a dimension member holds a string value;
  * a metric member holds a number, an array of numbers, or a statistic set
    object `{"Count": n, "Sum": s, "Min": a, "Max": b}`.
* Any other root members are ordinary log fields. CloudWatch Logs Insights
  can query them, but they do not become metric dimensions.

Example:

```json
{
  "_aws": {
    "Timestamp": 1700000000000,
    "CloudWatchMetrics": [
      {
        "Namespace": "Example/App",
        "Dimensions": [["Operation"]],
        "Metrics": [{"Name": "Latency", "Unit": "Milliseconds", "StorageResolution": 60}]
      }
    ]
  },
  "Operation": "Get",
  "RequestId": "abc-123",
  "Latency": 12.5
}
```

Real CloudWatch extracts metrics from such log events. The task's AWS endpoint
stores the log events but does not extract metrics, so the log event JSON is
the observable result.

## OTLP input

OTLP metrics requests use the public OpenTelemetry protocol definitions
(`opentelemetry/proto/collector/metrics/v1/metrics_service.proto` and
`opentelemetry/proto/metrics/v1/metrics.proto`). The environment provides the
Python bindings for these messages (package `opentelemetry-proto`, modules
`opentelemetry.proto.*`), together with `protobuf` and `grpcio`, for encoding
and decoding requests over HTTP and gRPC. Field names in the OTLP JSON
encoding are the lowerCamelCase forms of the protobuf field names.
