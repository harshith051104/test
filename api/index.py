from fastapi import FastAPI
from mangum import Mangum

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Hello Minion! FastAPI is running on Vercel 🚀"}

# This is critical — expose handler, not just app
handler = Mangum(app)
