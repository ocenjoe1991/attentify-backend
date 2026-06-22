"""Stripe integration endpoints (webhooks, checkout sessions)."""

from fastapi import APIRouter, Request, HTTPException

router = APIRouter()


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Handle incoming Stripe webhook events.

    TODO: Implement Stripe signature verification and event processing
    (e.g. payment_intent.succeeded, checkout.session.completed).
    """
    try:
        payload = await request.body()
        # TODO: Verify Stripe signature using stripe.Webhook.construct_event()
        # TODO: Process events and update subscription status
        return {"status": "received", "note": "Stripe webhook handler is not yet fully implemented"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")
