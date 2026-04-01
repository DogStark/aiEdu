from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import router

app = FastAPI(
    title="WordBloc AI Learning Agent",
    description="AI agent that studies kids' learning ability and recommends words for the WordBloc game.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your game's domain in production
    allow_methods=["*"],
    allow_headers=["*"]
)

app.include_router(router)


@app.get("/")
def root():
    return {
        "service": "WordBloc AI Learning Agent",
        "status": "running",
        "docs": "/docs"
    }
