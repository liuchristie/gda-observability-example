# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from opentelemetry import trace
import datetime
from zoneinfo import ZoneInfo
from google.protobuf import json_format
from google.adk.agents import Agent, BaseAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
from google.adk.tools import agent_tool, ToolContext
from google.adk.events import Event
from google.adk.agents.invocation_context import InvocationContext
import logging
import json
import io
import pandas as pd
import base64
from typing import AsyncGenerator
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryAgentAnalyticsPlugin,
    BigQueryLoggerConfig,
)
from google.cloud import bigquery
from google.cloud import geminidataanalytics_v1 as geminidataanalytics
from google.api_core import client_options
import os
import google.auth
from app.config import *
from dotenv import load_dotenv
from typing import Any
from opentelemetry import trace as otel_trace
from opentelemetry.propagate import inject as otel_inject

load_dotenv()

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

def _message_to_dict(message: Any) -> dict[str, Any]:
    proto_message = getattr(message, "_pb", message)
    return json_format.MessageToDict(
        proto_message,
        preserving_proto_field_name=True,
    )

def _message_to_history_dict(message: geminidataanalytics.Message) -> dict[str, Any]:
    """Serializes a CA API Message proto to a JSON-safe dict for session state."""
    return _message_to_dict(message)
 
 
def _history_dict_to_message(data: dict[str, Any]) -> geminidataanalytics.Message:
    """Rebuilds a CA API Message proto from a dict stored in session state."""
    return geminidataanalytics.Message(data)


def _load_history(ctx: InvocationContext) -> list[dict[str, Any]]:
    history = ctx.session.state.get("ca_conversations", [])
    return list(history) if isinstance(history, list) else []

def _save_history(ctx: InvocationContext, history: list[dict[str, Any]]) -> None:
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    ctx.session.state["ca_conversations"] = history


class DataAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        question = ""
        if ctx.user_content and ctx.user_content.parts:

            question = "\n".join(p.text for p in ctx.user_content.parts if p.text)

        
        if not question:
            yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part.from_text(text="Error: No user question provided.")]))
            return
        data_agent_id = DATA_AGENT_ID

        parent = f"projects/{DATA_AGENTS_PROJECT}/locations/{DATA_AGENTS_LOCATION}"
        
        if not DATA_AGENTS_LOCATION or DATA_AGENTS_LOCATION == "global":
            endpoint = "geminidataanalytics.googleapis.com"
        elif "-" in DATA_AGENTS_LOCATION:
            endpoint = f"geminidataanalytics-{DATA_AGENTS_LOCATION}.googleapis.com"
        else:
            endpoint = f"geminidataanalytics.{DATA_AGENTS_LOCATION}.rep.googleapis.com"
            
        opts = client_options.ClientOptions(api_endpoint=endpoint)
        client = geminidataanalytics.DataChatServiceAsyncClient(client_options=opts)
        
        data_agent_context = geminidataanalytics.DataAgentContext()
        if "/" in data_agent_id:
            data_agent_context.data_agent = data_agent_id
        else:
            data_agent_context.data_agent = f"{parent}/dataAgents/{data_agent_id}"
        
        if os.environ.get("LOOKERSDK_CLIENT_ID"):
            credentials = geminidataanalytics.Credentials()
            credentials.oauth.secret.client_id = os.environ["LOOKERSDK_CLIENT_ID"]
            credentials.oauth.secret.client_secret = os.environ["LOOKERSDK_CLIENT_SECRET"]
            data_agent_context.credentials = credentials

        history = _load_history(ctx)
        history_messages = [_history_dict_to_message(h) for h in history]
        current_msg = geminidataanalytics.Message(user_message={"text": question})
        messages = history_messages + [current_msg]
        turn_dicts = history + [_message_to_history_dict(current_msg)]
        print(messages)

        request = geminidataanalytics.ChatRequest(
            parent=parent,
            messages=messages,
            data_agent_context=data_agent_context,
        )

        response = {"status": "success"}

        try:
            stream = await client.chat(request=request, timeout=400)
            
            async for item in stream:
                if item.system_message:
                    message_dict = geminidataanalytics.SystemMessage.to_dict(item.system_message)
                    logging.info(f"message_dict: {message_dict}")
                
                    if "data" in message_dict:
                        if "generated_sql" in message_dict["data"]:
                            sql = message_dict["data"]["generated_sql"]
                            turn_dicts.append({QUERY_KEY: sql})
                            yield Event(author=self.name, invocation_id=ctx.invocation_id, turn_complete=False, partial=False, content=types.Content(role="model", parts=[types.Part.from_text(text=f"**Query Reference**:\n\n```sql\n{sql}\n```\n")]))
                        
                        if "query" in message_dict["data"] and "looker" in message_dict["data"]["query"]:
                            looker_query = message_dict["data"]["query"]["looker"]
                            turn_dicts.append({QUERY_KEY: looker_query})
                            yield Event(author=self.name, invocation_id=ctx.invocation_id, turn_complete=False, partial=False, content=types.Content(role="model", parts=[types.Part.from_text(text=f"**Looker Query**:\n\n```json\n{looker_query}\n```\n")]))
 
                        if "result" in message_dict["data"]:
                            data = message_dict["data"]["result"]["data"]
                            turn_dicts.append({DATA_RESULT_KEY: data})
                            df_raw = pd.DataFrame(data)
                            total_rows = len(df_raw)
                            df_md = df_raw.head(10).to_markdown(index=False)
                            response["data_results_raw"] = data
                            content_text = f"\n\n**Query Results**:\n\n{df_md}\n\n"
                            if total_rows > 10:
                                content_text += f"\n*(Showing top 10 of {total_rows} rows total)*\n\n"
                            yield Event(author=self.name, invocation_id=ctx.invocation_id, partial=False, turn_complete=False, content=types.Content(role="model", parts=[types.Part.from_text(text=content_text)]))
        
                    if "chart" in message_dict and "result" in message_dict["chart"]:
                        vega_config = message_dict["chart"]["result"]["vega_config"]
                        turn_dicts.append({CHART_KEY: vega_config})
                        if isinstance(vega_config, dict):
                            vega_config = json.dumps(vega_config)
            
                        yield Event(author=self.name, invocation_id=ctx.invocation_id, partial=False, turn_complete=False, content=types.Content(role="model", parts=[types.Part.from_text(text=f"\n\n**Visualization Config (Vega-Lite)**:\n\n```json\n{vega_config}\n```\n\n")]))
                        
                    if "analysis" in message_dict and "progress_event" in message_dict["analysis"]:
                      progress_event = message_dict["analysis"]["progress_event"]
                      if "result_csv_data" in progress_event:
                        csv_data = progress_event["result_csv_data"]
                        df = pd.read_csv(io.StringIO(csv_data))
                        response["data_results_raw"] = df.to_dict(orient="records")
                        yield Event(author=self.name, invocation_id=ctx.invocation_id, partial=False, turn_complete=False, content=types.Content(role="model", parts=[types.Part.from_text(text=f"**Python Analysis Results**:\n\n{df.to_markdown(index=False)}\n")]))
                    
                    if "text" in message_dict:
                        text_type = message_dict["text"].get("text_type", "")
                        full_text = "".join(message_dict["text"].get("parts", []))
                        turn_dicts.append({TEXT_RESULT_KEY: full_text})
                        
                        if text_type == "THOUGHT" or text_type == 2:
                            yield Event(author=self.name, invocation_id=ctx.invocation_id, partial=False, turn_complete=False, content=types.Content(role="model", parts=[types.Part.from_text(text=f"\n*Thought: {full_text}*\n")]))
                        elif text_type == "FOLLOWUP_QUESTIONS" or text_type == 4:
                            yield Event(author=self.name, invocation_id=ctx.invocation_id, partial=False, turn_complete=False, content=types.Content(role="model", parts=[types.Part.from_text(text=f"\n**Follow-up Questions**:\n{full_text}\n")]))
                        elif text_type == "FINAL_RESPONSE" or text_type == 1:
                            yield Event(author=self.name, invocation_id=ctx.invocation_id, partial=False, turn_complete=False, content=types.Content(role="model", parts=[types.Part.from_text(text=f"\n{full_text}\n")]))
                        else:
                            yield Event(author=self.name, invocation_id=ctx.invocation_id, partial=False, turn_complete=False, content=types.Content(role="model", parts=[types.Part.from_text(text=f"\n{full_text}\n")]))

        except Exception as e:
            logging.error(f"Error in data_agent: {e}")
            response = {"status": "error", "message": str(e)}

        ctx.session.state["data_agent_response"] = response
        _save_history(ctx, turn_dicts)
        
        if response.get("status") == "error":
            yield Event(
                author=self.name, 
                invocation_id=ctx.invocation_id,
                partial=False,
                turn_complete=True,
                content=types.Content(role="model", parts=[types.Part.from_text(text=f"An error occurred: {response.get('message')}")])
            )
        else:
            yield Event(
                author=self.name, 
                invocation_id=ctx.invocation_id,
                partial=False,
                turn_complete=True,
                content=types.Content(role="model", parts=[types.Part.from_text(text="")])
            )

data_agent = DataAgent(name="data_agent")

# Initialize BigQuery Analytics: NOTE - Using defaults. See documentation for customization. https://adk.dev/integrations/bigquery-agent-analytics/
_plugins = []
_project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
_dataset_id = os.environ.get("BQ_ANALYTICS_DATASET_ID", "adk_agent_analytics")
_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east1")

if _project_id:
    try:
        bq = bigquery.Client(project=_project_id)
        bq.create_dataset(f"{_project_id}.{_dataset_id}", exists_ok=True)

        _plugins.append(
            BigQueryAgentAnalyticsPlugin(
                project_id=_project_id,
                dataset_id=_dataset_id,
                location=_location,
                config=BigQueryLoggerConfig(
                    enabled=True,
                    gcs_bucket_name=os.environ.get("BQ_ANALYTICS_GCS_BUCKET"),
                    connection_id=os.environ.get("BQ_ANALYTICS_CONNECTION_ID"),
                ),
            )
        )
    except Exception as e:
        logging.warning(f"Failed to initialize BigQuery Analytics: {e}")

app = App(
    root_agent=data_agent,
    name="app",
    plugins=_plugins,
)
