from fastapi import FastAPI
from mangum import Mangum

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Hello Minion! FastAPI on Vercel is working 🚀"}

# Vercel expects a handler
handler = Mangum(app)
