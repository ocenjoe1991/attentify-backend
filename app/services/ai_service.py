import os
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import List, Dict, Any
import base64

#ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
    # other params...
)

EMAIL_ANALYSIS_PROMPT = (
    "You are a very talented order email analysis assistant."
    "The following text is an order, cancellation, or refund email encoded in Base64 from a Shopify customer. "
    "You must analyze BOTH the email title and the decoded email content to determine the order_id and request type. "
    "Check if the order_id field exists and is valid based on either the title, the content, or both.\n\n"
    "Common order id format is #CA0000 or #NZ0000, you should extrach correct order id as it is in email, not make new order. "
    "If the email is correct, output ONLY a valid JSON object (no markdown, no backticks, no explanations). "
    "The JSON must include these fields: order_id, type (either 'cancel' or 'refund'), status (1 if correct, otherwise 0), and msg. "
    "If the email is incorrect or missing an order ID, status must be 0 and msg should be a message requesting the order ID. "
    "If the email is correct, status must be 1 and msg should be an appropriate reply to the customer such as "
    "'Your order has been canceled.' or 'Your refund has been processed.'\n\n"
    "Analyze the following email:\n"
    "Title: {email_title}\n"
    "Content: {email_contents}"
)

prompt_template = PromptTemplate(
    input_variables=["email_title", "email_contents"],
    template=EMAIL_ANALYSIS_PROMPT
)

async def analyze_emails_with_ai_as_list(message: Dict[str, Any]):
    """
    Args:
        message: A Message object or dict, which has a 'messages' field containing ChatEntry dicts.
    Returns:
        List of AI JSON outputs, one per entry.
    """
    # Extract and base64 encode all message contents
    entries = message.get("messages", [])
    results = []
    for entry in entries:
        content = entry.get("content", "")
        # base64 encode the email body
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        # Prepare the prompt
        prompt = prompt_template.format(email_contents=encoded_content)
        # Call the LLM synchronously (langchain-anthropic currently does not support async)
        result = llm.invoke(prompt)
        # Just return the LLM's output (should be JSON)
        results.append({
            "entry_id": entry.get("metadata", {}).get("gmail_id"),  # or another unique key if not gmail
            "response": result.content
        })
    return results

async def analyze_emails_with_ai(message: Dict[str, Any]):
    """
    Args:
        message: A Message object or dict, which has a 'messages' field containing ChatEntry dicts.
    Returns:
        Single AI JSON output for the last 3 messages combined, or an error message.
    """
    try:
        title = message.get("title", "")
        entries = message.get("messages", [])
        if not entries:
            return {"error": "No messages found in input."}

        # Get the last 3 entries (or fewer if not enough)
        # last_entries = entries[-3:]
        try:
            combined_content = "\n\n".join(entry.get("content", "") for entry in entries)
        except Exception as content_exc:
            return {"error": f"Failed to combine message contents: {content_exc}"}

        try:
            encoded_content = base64.b64encode(combined_content.encode("utf-8")).decode("utf-8")
        except Exception as encode_exc:
            return {"error": f"Failed to encode contents: {encode_exc}"}
        
        try:
            prompt = prompt_template.format(email_title=title ,email_contents=encoded_content)
        except Exception as prompt_exc:
            return {"error": f"Failed to format prompt: {prompt_exc}"}
        try:
            result =  await llm.ainvoke(prompt)
        except Exception as llm_exc:
            return {"error": f"LLM invocation failed: {llm_exc}"}

        return result
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}