# Example BigQuery Agent Analytics Plugin Usage with Gemini Data Analytics API 


ADK implements OTel Semantic Conventions for GenAI by default, emitting standard OTLP format for end-to-end distributed tracing. If the default format is sufficient, you only need to set up the export: https://adk.dev/integrations/bigquery-agent-analytics/ 


The [ADK BigQuery Agent Analytics plugin](https://adk.dev/integrations/bigquery-agent-analytics/) provides a more robust implementation to capture more in-depth agent behavioral analysis. 
- This example uses default configs. See documentation for customization options. 

## Update 07/07/26

Replicate legacy Snowflake schema for agent observability:

1. [Create a Cloud Trace linked BigQuery dataset](https://docs.cloud.google.com/trace/docs/analytics-query-linked-dataset)

2. Inject the OpenTelemetry trace context into the gRPC metadata for your call to client.chat() (prevents GDA observability from creating a new trace_id):

```python
from opentelemetry import trace
from opentelemetry.propagate import inject as otel_inject


headers = {}
otel_inject(headers)
metadata = tuple(headers.items())

try:
  stream = await client.chat(request=request, timeout=400, metadata=metadata)
```

3. Create custom views in BigQuery (Legacy Snowflake Schema)
```sql
WITH trace_metrics AS (
  SELECT
    trace_id,
    SUM(CAST(JSON_VALUE(attributes['gen_ai.usage.input_tokens']) AS INT64)) AS input_tokens,
    SUM(CAST(JSON_VALUE(attributes['gen_ai.usage.output_tokens']) AS INT64)) AS output_tokens,
    SUM(CAST(JSON_VALUE(attributes['gen_ai.usage.cache_read.input_tokens']) AS INT64)) AS cache_read_tokens,
    ROUND(SUM(CAST(JSON_VALUE(attributes['gen_ai.usage.input_tokens']) AS INT64))/ 1000000.0 * 3.00, 4) AS input_cost_usd,
    ROUND( SUM(CAST(JSON_VALUE(attributes['gen_ai.usage.output_tokens']) AS INT64))/1000000.0 * 20.00, 4) AS output_cost_usd,
    TIMESTAMP_DIFF(MAX(end_time), MIN(start_time), MILLISECOND) AS response_time_ms,
    MAX(JSON_VALUE(attributes['gen_ai.agent.name'])) AS model_name
  FROM
    `your_project.your_trace_dataset.your_trace_table`
  WHERE
    name IN ('call_llm', 'generate_content', 'invocation')
  GROUP BY
    trace_id
),
adk_events AS (
  SELECT
    trace_id,
    session_id,
    invocation_id,
    user_id AS retailer_name,
    MIN(timestamp) AS event_timestamp,
    -- Extract the user question from the first received message
    MAX(CASE WHEN event_type = 'USER_MESSAGE_RECEIVED' THEN JSON_EXTRACT_SCALAR(content, '$.text_summary') ELSE NULL END) AS user_question,
    -- Extract the agent's response
    MAX(CASE WHEN event_type = 'AGENT_RESPONSE' THEN JSON_EXTRACT_SCALAR(content, '$.response') ELSE NULL END) AS chat_response,
    -- Infer application name from session metadata
    MAX(JSON_EXTRACT_SCALAR(attributes, '$.session_metadata.app_name')) AS origin_application,
    -- Record status
    MIN(status) AS status
  FROM
    `your_project.your_plugin_dataset.agent_events` 
  GROUP BY
    trace_id, session_id, invocation_id, user_id
)
SELECT
  a.event_timestamp AS TIMESTAMP,
  a.session_id AS thread_id,
  NULL AS parent_message_id, -- If supported in ADK, this can be mapped
  a.invocation_id AS message_id,
  a.user_question,
  IFNULL(a.origin_application, 'app') AS origin_application,
  a.retailer_name,
  IF(a.status = 'OK', 200, 500) AS response_status_code,
  t.response_time_ms,
  IFNULL(t.input_tokens, 0) AS input_tokens,
  IFNULL(t.output_tokens, 0) AS output_tokens,
  (IFNULL(t.input_tokens, 0) + IFNULL(t.output_tokens, 0)) AS total_tokens,
  IFNULL(t.cache_read_tokens, 0) AS cache_read_tokens,
  a.chat_response,
  IFNULL(t.model_name, 'ConversationalAnalyticsAgent') AS model_name,
  -- Cost Breakdown in USD ($3.00 / 1M input, $20.00 / 1M output)
  t.input_cost_usd,
  t.output_cost_usd,
  t.input_cost_usd + t.output_cost_usd AS total_cost_usd,
  COUNT(*) OVER (PARTITION BY a.session_id) AS thread_length
FROM
  adk_events a
LEFT JOIN
  trace_metrics t ON a.trace_id = t.trace_id
ORDER BY
  a.event_timestamp DESC;
```


