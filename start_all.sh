#!/usr/bin/env bash
# SketchCast AI — Start all active agents + Streamlit
# Usage: ./start_all.sh

set -e

echo "🚀 Starting SketchCast AI agents..."

# Agent 5: Audio Generation
uvicorn agent5_audio.main:app --reload --port 8003 &
echo "  ✅ Agent 5 (Audio)      → port 8003"

# Agent 6: Live Playback Engine
uvicorn agent6_player.main:app --reload --port 8004 &
echo "  ✅ Agent 6 (Player)     → port 8004"

# Agent 7: YouTube Export — DISABLED
# uvicorn agent7_youtube.main:app --reload --port 8005 &
echo "  🔒 Agent 7 (YouTube)    → disabled — enable when ready for YouTube launch"

# Agent 8: Q&A Interjection Handler
uvicorn agent8_qa.main:app --reload --port 8006 &
echo "  ✅ Agent 8 (Q&A)        → port 8006"

echo ""
echo "Starting Streamlit..."
streamlit run streamlit_app.py &
echo "  ✅ Streamlit UI         → port 8501"

echo ""
echo "All services started. Press Ctrl+C to stop all."
wait
