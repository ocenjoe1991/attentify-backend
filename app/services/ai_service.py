import os
import asyncio
import logging
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import List, Dict, Any
import base64

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise RuntimeError("GOOGLE_API_KEY environment variable is not set")

# Models to try in order (first success wins)
MODEL_CHAIN = [
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
]

_logger = logging.getLogger("attentify.ai")


async def _try_invoke(prompt, model_name: str):
    """Try invoking a specific Gemini model. Returns result or raises."""
    llm = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=GOOGLE_API_KEY,
        temperature=0,
        max_tokens=256,
        timeout=60,
        max_retries=1,
    )
    return await llm.ainvoke(prompt)


async def invoke_with_fallback(prompt):
    """Try each model in MODEL_CHAIN. On rate-limit, wait then retry same model."""
    last_error = None
    for model_name in MODEL_CHAIN:
        for attempt in range(2):  # 2 attempts per model
            try:
                _logger.info("Trying model: %s (attempt %d)", model_name, attempt + 1)
                return await _try_invoke(prompt, model_name)
            except Exception as e:
                error_str = str(e)
                is_rate_limit = "429" in error_str
                _logger.warning("Model %s attempt %d failed: %s", model_name, attempt + 1, error_str[:120])
                last_error = e
                if is_rate_limit and attempt == 0:
                    # Wait before retrying same model
                    import re
                    match = re.search(r"retry in (\d+\.?\d*)s", error_str)
                    wait = float(match.group(1)) if match else 30
                    _logger.info("Rate limited, waiting %.0fs before retry...", wait)
                    await asyncio.sleep(wait)
                    continue
                break  # Not rate limit or already retried, try next model
    raise last_error

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


def _get_user_friendly_error(error_text: str) -> str:
    """Return a user-safe error message based on the raw API error."""
    if "429" in error_text or "quota" in error_text.lower() or "rate" in error_text.lower():
        return "AI service is temporarily unavailable (rate limit). Please try again later."
    if "401" in error_text or "403" in error_text:
        return "AI service configuration error. Please contact support."
    if "api_key" in error_text.lower():
        return "AI service configuration error. Please contact support."
    return "AI analysis could not be completed at this time."

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
            result = await invoke_with_fallback(prompt)
        except Exception as llm_exc:
            error_str = str(llm_exc)
            # Extract key details for logging
            error_detail = {
                "error": "AI service error",
                "raw_error": error_str[:500],
            }
            if "429" in error_str:
                error_detail["error"] = "AI_RATE_LIMIT"
                error_detail["reason"] = "Gemini API quota exceeded (429)"
                # Extract retry delay if available
                import re as _re
                retry_match = _re.search(r'retry in (\d+\.?\d*)s', error_str)
                if retry_match:
                    error_detail["retry_after_seconds"] = float(retry_match.group(1))
                # Extract model name
                model_match = _re.search(r'model:\s*(\S+)', error_str)
                if model_match:
                    error_detail["model"] = model_match.group(1)
            elif "401" in error_str or "403" in error_str:
                error_detail["error"] = "AI_AUTH_ERROR"
                error_detail["reason"] = "Invalid or expired API key"
            elif "api_key" in error_str.lower():
                error_detail["error"] = "AI_AUTH_ERROR"
                error_detail["reason"] = "API key not configured"
            else:
                error_detail["error"] = "AI_UNKNOWN_ERROR"
                error_detail["reason"] = error_str[:200]
            # Still return user-friendly message for UI
            error_detail["msg"] = _get_user_friendly_error(error_str)
            return error_detail

        return result
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}