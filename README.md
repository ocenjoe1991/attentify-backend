# Attentify Backend

This is the backend API server for **Attentify**, a unified, AI-powered customer support hub for Shopify stores.  
Built with **FastAPI**, connected to **MongoDB**, and supporting real-time communication via Socket.io.

---

## ✨ Features

- JWT-based authentication & user management
- Unified inbox (email, SMS, calls)
- Shopify, Twilio, Gmail, Stripe integrations
- AI-powered message handling (OpenAI, Claude, etc.)
- Webhooks for store and communication events
- Subscription and billing management
- Real-time updates with Socket.io

---

## 🚀 Getting Started

### 1. **Clone the repository**

```bash
git clone https://github.com/your-org/attentify.git
cd attentify/backend
```

### 2. **Install dependencies**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. **Environment Variables**

Create a `.env` file (or set environment variables) for configuration:

```env
MONGODB_URI=mongodb://localhost:27017/attentify
SECRET_KEY=your-secret-key
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

OPENAI_API_KEY=your-openai-key
SHOPIFY_API_KEY=your-shopify-key
TWILIO_ACCOUNT_SID=your-twilio-sid
TWILIO_AUTH_TOKEN=your-twilio-token
GMAIL_CLIENT_ID=your-gmail-client-id
GMAIL_CLIENT_SECRET=your-gmail-client-secret
STRIPE_SECRET_KEY=your-stripe-key
# ... other keys as needed
```

### 4. **Run the server**

```bash
uvicorn app.main:app --reload
```

Visit [http://localhost:8000/docs](http://localhost:8000/docs) for the Swagger API documentation.

---

## 🗂️ Project Structure

```
backend/
├── app/
│   ├── main.py                  # FastAPI app entry point
│   ├── api/                     # Routers for each domain
│   ├── core/                    # Config, security, enums
│   ├── models/                  # DB models
│   ├── schemas/                 # Pydantic schemas
│   ├── services/                # External integrations & logic
│   ├── socket/                  # Socket.io events
│   └── utils/                   # Helpers
├── requirements.txt
└── README.md
```

---

## 🧪 Testing

You can use [pytest](https://docs.pytest.org/) for testing:

```bash
pip install pytest
pytest
```

---

## 🛠️ Useful Commands

- **Run with hot reload:**  
  `.\venv\Scripts\Activate.ps1`
  `uvicorn app.main:socket_app --reload`
- **Run in production (example):**  
  `gunicorn -k uvicorn.workers.UvicornWorker app.main:socket_app`

---

## 📄 License

MIT

---

## 👩‍💻 Contributing

See the main repository [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.
