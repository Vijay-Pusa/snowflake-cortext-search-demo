import streamlit as st
import json
import _snowflake
from snowflake.snowpark.context import get_active_session

# Get Snowflake session
session = get_active_session()

# Constants
API_ENDPOINT = "/api/v2/cortex/agent:run"
API_TIMEOUT = 50000
CORTEX_SEARCH_SERVICES = "HIVE_TO_SF_MGN.FUNCTION_TRANSLATOR.HIVE_TO_SF_TRANSLATOR"
SEMANTIC_MODELS = "@hive_to_sf_mgn.function_translator.STAGE/hive_to_sf_func_translator.yaml"

def run_snowflake_query(query):
    try:
        df = session.sql(query.replace(';', ''))
        return df
    except Exception as e:
        st.error(f"Error executing SQL: {str(e)}")
        return None

def snowflake_api_call(query: str, limit: int = 10):
    payload = {
        "model": "claude-3-5-sonnet",
        "response-instruction": "You will always maintain a serious tone and provide concise response, you will always refer the user as Agent.",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": query}]}
        ],
        "tools": [
            {"tool_spec": {"type": "cortex_analyst_text_to_sql", "name": "analyst1"}},
            {"tool_spec": {"type": "cortex_search", "name": "search1"}}
        ],
        "tool_resources": {
            "analyst1": {"semantic_model_file": SEMANTIC_MODELS},
            "search1": {
                "name": CORTEX_SEARCH_SERVICES,
                "max_results": limit,
                "id_column": "function_mapping_id"
            }
        }
    }
    try:
        resp = _snowflake.send_snow_api_request("POST", API_ENDPOINT, {}, {}, payload, None, API_TIMEOUT)
        if resp["status"] != 200:
            st.error(f"❌ HTTP Error: {resp['status']} - {resp.get('reason', 'Unknown reason')}")
            return None
        return json.loads(resp["content"])
    except Exception as e:
        st.error(f"Error making request: {str(e)}")
        return None

def process_sse_response(response):
    fallback_text_parts = []
    sql = ""
    citations = []

    if not response or isinstance(response, str):
        return "", "", []

    try:
        for event in response:
            if event.get("event") == "message.delta":
                data = event.get("data", {})
                delta = data.get("delta", {})
                for item in delta.get("content", []):
                    if item.get("type") == "tool_results":
                        for result in item.get("tool_results", {}).get("content", []):
                            json_data = result.get("json", {})
                            sql = json_data.get("sql", sql)
                            text = json_data.get("text", "")
                            if text:
                                fallback_text_parts.append(text)
                            for search_result in json_data.get("searchResults", []):
                                citations.append({
                                    "source_id": search_result.get("source_id", ""),
                                    "doc_id": search_result.get("doc_id", "")
                                })
                    elif item.get("type") == "text":
                        candidate = item.get("text", "")
                        if candidate.lower().startswith("i don't know"):
                            continue
                        fallback_text_parts.append(candidate)
    except Exception as e:
        st.error(f"Error processing events: {str(e)}")

    fallback_text = " ".join(fallback_text_parts).strip()
    return fallback_text, sql, citations


def process_error_fix_suggestion(doc_id):
    query = f"""
        SELECT ERROR_REASON, ERROR_FIX_SUGGESTION, SF_FUNC_NAME
        FROM error_info
        WHERE FUNCTION_MAPPING_ID = '{doc_id.replace("'", "''")}'
    """
    result = run_snowflake_query(query)
    if result is not None:
        pdf = result.to_pandas()
        if not pdf.empty:
    
            # Get the three required columns: error_reason, ERROR_FIX_SUGGESTION, SF_FUNC_NAME
            error_reason = pdf["ERROR_REASON"].iloc[0]
            error_fix_suggestion = pdf["ERROR_FIX_SUGGESTION"].iloc[0]
            sf_func_name = pdf["SF_FUNC_NAME"].iloc[0]
            
            # Combine the results in a single message
            response = f"### Error Reason:\n{error_reason}\n\n### Suggested Fix:\n{error_fix_suggestion}\n\n### Snowflake Function Name:\n{sf_func_name}"
            return response
    return "No specific error fix suggestion was found."

def fetch_function_details(doc_id):
    query = f"""
        SELECT
            sf.SF_FUNC_NAME,
            sf.sf_func_desc,
            sf.sf_return_val,
            sf.FUNCTION_MAPPING_ID,
            sf.sf_func_SYNTAX
        FROM sf_FUNC sf
        JOIN hive_FUNC hf ON sf.function_mapping_id = hf.function_mapping_id
        WHERE sf.function_mapping_id = '{doc_id.replace("'", "''")}'
    """
    result = run_snowflake_query(query)
    return result.to_pandas() if result else None

def main():
    st.title("Hive to Snowflake Function Translator")
    with st.sidebar:
        if st.button("New Conversation", key="new_chat"):
            st.session_state.messages = [{"role": "assistant", "content": "Hi Data Engineer, I am here to save your time. Please tell me which hive function you are struggling to convert to Snowflake?"}]
            st.rerun()

    if "messages" not in st.session_state:
        st.session_state.messages = [{"role": "assistant", "content": "Hi Data Engineer, I am here to save your time. Please tell me which hive function you are struggling to convert to Snowflake?"}]

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"], avatar="👩‍💻" if msg["role"] == "user" else "👸"):
            st.markdown(msg["content"].replace("•", "\n\n"))

    if query := st.chat_input("What is your request?"):
        st.session_state.messages.append({"role": "user", "content": query})
        with st.chat_message("user", avatar="👩‍💻"):
            st.markdown(query)

        with st.spinner("Processing your request..."):
            response = snowflake_api_call(query, limit=1)
            text, sql, citations = process_sse_response(response)

            # Show function details if user asks for alternative
            if any(word in query.lower() for word in ["alternative", "equivalent", "replacement"]):
                if text:
                    st.session_state.messages.append({"role": "assistant", "content": text})
                    with st.chat_message("assistant", avatar="👸"):
                        st.markdown(text.replace("•", "\n\n"))

                if citations:
                    st.write("### The perfect Snowflake Function does exist. Below are the details:")
                    for citation in citations:
                        doc_id = citation.get("doc_id", "")
                        if doc_id:
                            df = fetch_function_details(doc_id)
                            if df is not None:
                                st.dataframe(df)

            # Show error suggestion if user asks about an error
            elif "error" in query.lower():
                if citations:
                    doc_id = citations[0].get("doc_id", "")
                    suggestion = process_error_fix_suggestion(doc_id)
                    st.session_state.messages.append({"role": "assistant", "content": suggestion})
                    with st.chat_message("assistant", avatar="👸"):
                        st.markdown(suggestion)

            # Show fallback if it's neither of above
            elif text:
                text = text.replace("【†1†】", "")
                st.session_state.messages.append({"role": "assistant", "content": text})
                with st.chat_message("assistant", avatar="👸"):
                    st.markdown(text.replace("•", "\n\n"))

            # Show SQL and results if any
            if sql:
                with st.chat_message("assistant", avatar="👩‍💻"):
                    #st.markdown("### Generated SQL")
                    #st.code(sql, language="sql")
                    sql_result = run_snowflake_query(sql)
                    if sql_result:
                        st.write("### As per the data engineer experts, we think below details can solve your problem")
                        st.dataframe(sql_result.to_pandas())

if __name__ == "__main__":
    main()
