from api.agent_router import router as agent_router
from fastapi import FastAPI
import uvicorn
from core.lifespan import lifespan
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(agent_router)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
