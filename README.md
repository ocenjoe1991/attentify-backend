# Attentify Backend

This is the backend API server for **Attentify**, a unified, AI-powered customer support hub for Shopify stores.
It is built with **FastAPI**, uses **MongoDB**, and supports real-time communication through Socket.io.

## Features

- JWT-based authentication and user management
- Unified inbox for email, SMS, and calls
- Shopify, Twilio, Gmail, and Stripe integrations
- AI-powered message handling
- Webhooks for store and communication events
- Subscription and billing management
- Real-time updates with Socket.io

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/your-org/attentify.git
cd attentify/backend
```

### 2. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file, or set environment variables, for configuration:

```env
MONGODB_URI=mongodb://localhost:27017/attentify
SECRET_KEY=your-secret-key
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

FRONTEND_URL=http://localhost:5173
BACKEND_URL=http://localhost:8000

GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GOOGLE_AUTH_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/gmail/oauth/callback

OPENAI_API_KEY=your-openai-key
SHOPIFY_API_KEY=your-shopify-key
TWILIO_ACCOUNT_SID=your-twilio-sid
TWILIO_AUTH_TOKEN=your-twilio-token
STRIPE_SECRET_KEY=your-stripe-key
```

For Google OAuth, add both redirect URIs above to the OAuth client in Google Cloud Console.

### 4. Run the server

```bash
uvicorn app.main:app --reload
```

Visit [http://localhost:8000/docs](http://localhost:8000/docs) for the Swagger API documentation.

## Project Structure

```text
backend/
  app/
    main.py       # FastAPI app entry point
    api/          # Routers for each domain
    core/         # Config, security, and enums
    models/       # Database models
    schemas/      # Pydantic schemas
    services/     # External integrations and business logic
    socket/       # Socket.io events
    utils/        # Helpers
  requirements.txt
  README.md
```

## Testing

You can use [pytest](https://docs.pytest.org/) for testing:

```bash
pip install pytest
pytest
```

## Useful Commands

- Run with hot reload:
  `.\venv\Scripts\Activate.ps1`
  `uvicorn app.main:socket_app --reload`
- Run in production:
  `gunicorn -k uvicorn.workers.UvicornWorker app.main:socket_app`

## Contributing

See the main repository [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

## License

MIT
