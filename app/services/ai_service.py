import os
import re
import logging
from langchain_core.prompts import PromptTemplate
from typing import List, Dict, Any

_logger = logging.getLogger("attentify.ai")

# Try Gemini first, fall back to Groq (both have free tiers)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

if not GOOGLE_API_KEY and not GROQ_API_KEY:
    raise RuntimeError("Either GOOGLE_API_KEY or GROQ_API_KEY must be set")


async def invoke_with_fallback(prompt):
    """Try Gemini once, if 429 go straight to Groq."""

    # ---- Try Gemini (single attempt) ----
    if GOOGLE_API_KEY:
        from langchain_google_genai import ChatGoogleGenerativeAI
        for model_name in ["gemini-2.0-flash-lite", "gemini-2.0-flash"]:
            try:
                _logger.info("Gemini %s", model_name)
                llm = ChatGoogleGenerativeAI(
                    model=model_name, google_api_key=GOOGLE_API_KEY,
                    temperature=0, max_tokens=256, timeout=15, max_retries=0,
                )
                return await llm.ainvoke(prompt)
            except Exception as e:
                err = str(e)
                if "429" in err:
                    _logger.warning("Gemini %s rate-limited, skipping to Groq", model_name)
                    break  # Stop trying Gemini, go to Groq
                _logger.warning("Gemini %s failed: %s", model_name, err[:120])
                # Non-429 error, try next Gemini model
                continue

    # ---- Groq (free, fast, reliable) ----
    if GROQ_API_KEY:
        from langchain_groq import ChatGroq
        _logger.info("Using Groq (llama-3.1-8b-instant)")
        llm = ChatGroq(
            model="llama-3.1-8b-instant", api_key=GROQ_API_KEY,
            temperature=0, max_tokens=256, timeout=60, max_retries=2,
        )
        return await llm.ainvoke(prompt)

    raise RuntimeError("All AI models failed. Check API keys.")

EMAIL_ANALYSIS_PROMPT = (
    "Task: Extract order ID from this email. Reply with ONLY a JSON object, nothing else.\n"
    "Format: {{\"order_id\":\"#...\",\"type\":\"refund or cancel\",\"status\":1 or 0,\"msg\":\"reply\"}}\n"
    "If no order found: {{\"order_id\":\"\",\"type\":\"\",\"status\":0,\"msg\":\"Please provide your order number.\"}}\n\n"
    "Email title: {email_title}\n"
    "Email content: {email_contents}"
)

prompt_template = PromptTemplate(
    input_variables=["email_title", "email_contents"],
    template=EMAIL_ANALYSIS_PROMPT
)


def _get_user_friendly_error(error_text: str) -> str:
    """Return a user-safe error message based on the raw API error."""
    if "429" in error_text or "quota" in error_text.lower() or "rate" in error_text.lower():
        return "AI service is temporarily unavailable (rate limit). Please try again later."
    if "413" in error_text or "too large" in error_text.lower():
        return "Email content is too large for analysis. Please try again later."
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

def _strip_html(text: str) -> str:
    """Remove HTML tags, decode entities, collapse whitespace."""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


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
            combined_content = "\n".join(
                _strip_html(entry.get("content", "")) for entry in entries
            )
            # Keep first 3000 chars of plain text (enough for order ID extraction)
            if len(combined_content) > 3000:
                combined_content = combined_content[:3000]
        except Exception as content_exc:
            return {"error": f"Failed to combine message contents: {content_exc}"}

        try:
            prompt = prompt_template.format(email_title=title, email_contents=combined_content)
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