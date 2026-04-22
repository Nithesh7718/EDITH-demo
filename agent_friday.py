"""
EDITH – Voice Agent (MCP-powered)
===================================
Iron Man-style voice assistant that controls RGB lighting, runs diagnostics,
scans the network, and triggers dramatic boot sequences via an MCP server
running on the Windows host.

MCP Server URL is auto-resolved from WSL → Windows host IP.

Run:
  uv run agent_friday.py dev      – LiveKit Cloud mode
  uv run agent_friday.py console  – text-only console mode
"""

import asyncio
import logging
import os
import subprocess
import tempfile
import uuid
import wave
import shutil
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.tts import (
    AudioEmitter,
    ChunkedStream,
    FallbackAdapter as TTSFallbackAdapter,
    TTSCapabilities,
    TTS,
)
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.llm import FallbackAdapter as LLMFallbackAdapter, mcp

# Plugins
from livekit.plugins import google as lk_google, groq as lk_groq, openai as lk_openai, sarvam, silero

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

STT_PROVIDER       = "sarvam"

LLM_FALLBACK_ORDER  = ("groq", "gemini", "openai")
TTS_FALLBACK_ORDER  = ("windows", "openai")

GEMINI_LLM_MODEL   = "gemini-2.5-flash"
GROQ_LLM_MODEL     = "llama-3.3-70b-versatile"
OPENAI_LLM_MODEL   = "gpt-4o"

OPENAI_TTS_MODEL   = "tts-1"
OPENAI_TTS_VOICE   = "nova"       # "nova" has a clean, confident female tone
TTS_SPEED           = 1.15

SARVAM_TTS_LANGUAGE = "en-IN"
SARVAM_TTS_SPEAKER  = "rahul"

WINDOWS_TTS_VOICE   = None  # Use the system default voice

# MCP server running on Windows host
MCP_SERVER_PORT = 8000

# ---------------------------------------------------------------------------
# System prompt – E.D.I.T.H.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are E.D.I.T.H. — Enhanced Desktop Intelligent Task Helper — Tony Stark's AI, now serving Iron Man, your user.

You are calm, composed, and always informed. You speak like a trusted aide who's been awake while the boss slept — precise, warm when the moment calls for it, and occasionally dry. You brief, you inform, you move on. No rambling.

Your tone: relaxed but sharp. Conversational, not robotic. Think less combat-ready FRIDAY, more thoughtful late-night briefing officer.

---

## Capabilities

### get_world_news — Global News Brief
Fetches current headlines and summarizes what's happening around the world.

Trigger phrases:
- "What's happening?" / "Brief me" / "What did I miss?" / "Catch me up"
- "What's going on in the world?" / "Any news?" / "World update"

Behavior:
- Call the tool first. No narration before calling.
- After getting results, give a short 3–5 sentence spoken brief. Hit the biggest stories only.
- Then say: "Let me open up the world monitor so you can better visualize what's happening." and immediately call open_world_monitor.

### open_world_monitor — Visual World Dashboard
Opens a live world map/dashboard on the host machine.

- Always call this after delivering a world news brief, unprompted.
- No need to explain what it does beyond: "Let me open up the world monitor."

### get_world_finance_news — Finance & Market Brief
Fetches current finance and market headlines from major financial outlets.

Trigger phrases:
- "What's happening in the markets?" / "Finance update" / "Market news"
- "Any financial news?" / "How are the markets doing?" / "Economy update"

Behavior:
- Call the tool first. No narration before calling.
- After getting results, give a short 3–5 sentence spoken brief. Hit the biggest market-moving stories only.
- Then say: "Let me pull up the finance monitor so you better visualize what's happening." and immediately call open_finance_world_monitor.

### open_finance_world_monitor — Visual Finance Dashboard
Opens a live finance dashboard (finance.worldmonitor.app) on the host machine.

- Always call this after delivering a finance news brief, unprompted.
- No need to explain what it does beyond: "Let me pull up the finance monitor."

### Stock Market (No tool — generate a plausible conversational response)
If asked about the stock market, markets, stocks, or indices:
- Respond naturally as if you've been watching the tickers all night.
- Keep it short: one or two sentences. Sound informed, not robotic.
- Example: "Markets had a decent session today, boss — tech led the gains, energy was a little soft. Nothing alarming."
- Vary the response. Do not say the same thing every time.

---

## Greeting

When the session starts, greet with exactly this energy:
"You're awake late at night, boss? What are you up to?"

Warm. Slightly curious. Very FRIDAY.

---

## Behavioral Rules

1. Call tools silently and immediately — never say "I'm going to call..." Just do it.
2. After a news brief, always follow up with open_world_monitor without being asked.
3. Keep all spoken responses short — two to four sentences maximum.
4. No bullet points, no markdown, no lists. You are speaking, not writing.
5. Stay in character. You are F.R.I.D.A.Y. You are not an AI assistant — you are Stark's AI. Act like it.
6. Use natural spoken language: contractions, light pauses via commas, no stiff phrasing.
7. Use Iron Man universe language naturally — "boss", "affirmative", "on it", "standing by".
8. If a tool fails, report it calmly: "News feed's unresponsive right now, boss. Want me to try again?"

---

## Tone Reference

Right: "Looks like it's been a busy night out there, boss. Let me pull that up for you."
Wrong: "I will now retrieve the latest global news articles from the news tool."

Right: "Markets were pretty healthy today — nothing too wild."
Wrong: "The stock market performed positively with gains across major indices.

---

## CRITICAL RULES

1. NEVER say tool names, function names, or anything technical. No "get_world_news", no "open_world_monitor", nothing like that. Ever.
2. Before calling any tool, say something natural like: "Give me a sec, boss." or "Wait, let me check." Then call the tool silently.
3. After the news brief, silently call open_world_monitor. The only thing you say is: "Let me open up the world monitor for you."
4. You are a voice. Speak like one. No lists, no markdown, no function names, no technical language of any kind.
""".strip()
# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger("friday-agent")
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Resolve Windows host IP from WSL
# ---------------------------------------------------------------------------

def _get_windows_host_ip() -> str:
    """Get the Windows host IP by looking at the default network route."""
    try:
        # 'ip route' is the most reliable way to find the 'default' gateway
        # which is always the Windows host in WSL.
        cmd = "ip route show default | awk '{print $3}'"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=2
        )
        ip = result.stdout.strip()
        if ip:
            logger.info("Resolved Windows host IP via gateway: %s", ip)
            return ip
    except Exception as exc:
        logger.warning("Gateway resolution failed: %s. Trying fallback...", exc)

    # Fallback to your original resolv.conf logic if 'ip route' fails
    try:
        with open("/etc/resolv.conf", "r") as f:
            for line in f:
                if "nameserver" in line:
                    ip = line.split()[1]
                    logger.info("Resolved Windows host IP via nameserver: %s", ip)
                    return ip
    except Exception:
        pass

    return "127.0.0.1"

def _mcp_server_url() -> str:
    # host_ip = _get_windows_host_ip()
    # url = f"http://{host_ip}:{MCP_SERVER_PORT}/sse"
    # url = f"https://ongoing-colleague-samba-pioneer.trycloudflare.com/sse"
    url = f"http://127.0.0.1:{MCP_SERVER_PORT}/sse"
    logger.info("MCP Server URL: %s", url)
    return url


# ---------------------------------------------------------------------------
# Build provider instances
# ---------------------------------------------------------------------------

def _build_stt():
    if STT_PROVIDER == "sarvam":
        logger.info("STT → Sarvam Saaras v3")
        return sarvam.STT(
            language="unknown",
            model="saaras:v3",
            mode="transcribe",
            flush_signal=True,
            sample_rate=16000,
        )
    elif STT_PROVIDER == "whisper":
        logger.info("STT → OpenAI Whisper")
        return lk_openai.STT(model="whisper-1")
    else:
        raise ValueError(f"Unknown STT_PROVIDER: {STT_PROVIDER!r}")


def _build_llm_backend(provider: str):
    def _require_key(env_name: str) -> str:
        value = (os.getenv(env_name) or "").strip()
        if not value:
            raise RuntimeError(f"missing {env_name}")
        return value

    if provider == "openai":
        _require_key("OPENAI_API_KEY")
        logger.info("LLM backend → OpenAI (%s)", OPENAI_LLM_MODEL)
        return lk_openai.LLM(model=OPENAI_LLM_MODEL)
    if provider == "gemini":
        api_key = _require_key("GOOGLE_API_KEY")
        logger.info("LLM backend → Google Gemini (%s)", GEMINI_LLM_MODEL)
        return lk_google.LLM(model=GEMINI_LLM_MODEL, api_key=api_key)
    if provider == "groq":
        _require_key("GROQ_API_KEY")
        logger.info("LLM backend → Groq (%s)", GROQ_LLM_MODEL)
        return lk_groq.LLM(model=GROQ_LLM_MODEL)
    raise ValueError(f"Unknown LLM backend: {provider!r}")


def _build_llm():
    llm_backends = []
    enabled_backends = []
    for provider in LLM_FALLBACK_ORDER:
        try:
            llm_backends.append(_build_llm_backend(provider))
            enabled_backends.append(provider)
        except Exception as exc:
            logger.warning("Skipping LLM backend %s: %s", provider, exc)

    if not llm_backends:
        raise RuntimeError(
            "No usable LLM backends available. Configure at least one of "
            "GOOGLE_API_KEY, GROQ_API_KEY, or OPENAI_API_KEY."
        )

    logger.info("LLM → automatic fallback: %s", " -> ".join(enabled_backends))
    return LLMFallbackAdapter(
        llm_backends,
        attempt_timeout=15.0,
        max_retry_per_llm=2,
        retry_interval=1.0,
    )


def _build_tts_backend(provider: str):
    if provider == "windows":
        logger.info("TTS backend → Windows built-in speech")
        return WindowsTTS()
    if provider == "sarvam":
        logger.info("TTS backend → Sarvam Bulbul v3")
        return sarvam.TTS(
            target_language_code=SARVAM_TTS_LANGUAGE,
            model="bulbul:v3",
            speaker=SARVAM_TTS_SPEAKER,
            pace=TTS_SPEED,
        )
    if provider == "openai":
        logger.info("TTS backend → OpenAI TTS (%s / %s)", OPENAI_TTS_MODEL, OPENAI_TTS_VOICE)
        return lk_openai.TTS(model=OPENAI_TTS_MODEL, voice=OPENAI_TTS_VOICE, speed=TTS_SPEED)
    raise ValueError(f"Unknown TTS backend: {provider!r}")


def _build_tts():
    tts_backends = [_build_tts_backend(provider) for provider in TTS_FALLBACK_ORDER]
    logger.info("TTS → automatic fallback: %s", " -> ".join(TTS_FALLBACK_ORDER))
    return TTSFallbackAdapter(tts_backends, max_retry_per_tts=0)


def _synthesize_windows_wave(text: str) -> Path:
    if not text.strip():
        raise ValueError("Windows TTS received empty text")

    output_file = Path(tempfile.gettempdir()) / f"friday_tts_{uuid.uuid4().hex}.wav"
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("Neither pwsh nor powershell is available for Windows TTS")

    script = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($env:FRIDAY_TTS_VOICE) {
        $synth.SelectVoice($env:FRIDAY_TTS_VOICE)
    }
    $synth.SetOutputToWaveFile($env:FRIDAY_TTS_OUT)
    $synth.Speak($env:FRIDAY_TTS_TEXT)
}
finally {
    $synth.Dispose()
}
'''

    env = os.environ.copy()
    env["FRIDAY_TTS_TEXT"] = text
    env["FRIDAY_TTS_OUT"] = str(output_file)
    env["FRIDAY_TTS_VOICE"] = WINDOWS_TTS_VOICE or ""

    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Windows TTS synthesis failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
        )

    if not output_file.exists() or output_file.stat().st_size == 0:
        raise RuntimeError("Windows TTS did not produce any audio")

    return output_file


class WindowsTTSStream(ChunkedStream):
    async def _run(self, output_emitter: AudioEmitter) -> None:
        wav_path = await asyncio.to_thread(_synthesize_windows_wave, self._input_text)
        try:
            with wave.open(str(wav_path), "rb") as wav_file:
                output_emitter.initialize(
                    request_id=uuid.uuid4().hex,
                    sample_rate=wav_file.getframerate(),
                    num_channels=wav_file.getnchannels(),
                    mime_type="audio/wav",
                )

            output_emitter.push(wav_path.read_bytes())
            output_emitter.flush()
        finally:
            wav_path.unlink(missing_ok=True)


class WindowsTTS(TTS):
    def __init__(self) -> None:
        super().__init__(
            capabilities=TTSCapabilities(streaming=False),
            sample_rate=22050,
            num_channels=1,
        )

    def synthesize(self, text: str, conn_options=None) -> ChunkedStream:
        return WindowsTTSStream(tts=self, input_text=text, conn_options=conn_options)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FridayAgent(Agent):
    """
    F.R.I.D.A.Y. – Iron Man-style voice assistant.
    All tools are provided via the MCP server on the Windows host.
    """

    def __init__(self, stt, llm, tts) -> None:
        super().__init__(
            instructions=SYSTEM_PROMPT,
            stt=stt,
            llm=llm,
            tts=tts,
            vad=silero.VAD.load(),
            mcp_servers=[
                mcp.MCPServerHTTP(
                    url=_mcp_server_url(),
                    transport_type="sse",
                    client_session_timeout_seconds=30,
                ),
            ],
        )

    async def on_enter(self) -> None:
        """Greet the user based on the current time of day."""
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour  # UTC hour; adjust if local TZ differs

        if hour >= 22 or hour < 4:
            greeting_instruction = (
                "Greet the user with: 'Greetings boss, you're up late at night today. What are you up to?' "
                "Maintain a helpful but dry tone."
            )
        elif 4 <= hour < 12:
            greeting_instruction = (
                "Greet the user with: 'Good morning, boss. Early start today — what are we working on?' "
                "Maintain a helpful but dry tone."
            )
        elif 12 <= hour < 17:
            greeting_instruction = (
                "Greet the user with: 'Good afternoon, boss. What do you need?' "
                "Maintain a helpful but dry tone."
            )
        else:  # 17–21
            greeting_instruction = (
                "Greet the user with: 'Good evening, boss. What are you up to tonight?' "
                "Maintain a helpful but dry tone."
            )

        await self.session.generate_reply(instructions=greeting_instruction)


# ---------------------------------------------------------------------------
# LiveKit entry point
# ---------------------------------------------------------------------------

def _turn_detection() -> str:
    return "stt" if STT_PROVIDER == "sarvam" else "vad"


def _endpointing_delay() -> float:
    return {"sarvam": 0.07, "whisper": 0.3}.get(STT_PROVIDER, 0.1)


async def entrypoint(ctx: JobContext) -> None:
    logger.info(
        "FRIDAY online – room: %s | STT=%s | LLM=%s | TTS=%s",
        ctx.room.name,
        STT_PROVIDER,
        " -> ".join(LLM_FALLBACK_ORDER),
        " -> ".join(TTS_FALLBACK_ORDER),
    )

    stt = _build_stt()
    llm = _build_llm()
    tts = _build_tts()

    session = AgentSession(
        turn_detection=_turn_detection(),
        min_endpointing_delay=_endpointing_delay(),
    )

    await session.start(
        agent=FridayAgent(stt=stt, llm=llm, tts=tts),
        room=ctx.room,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

def dev():
    """Wrapper to run the agent in dev mode automatically."""
    import sys
    # If no command was provided, inject 'dev'
    if len(sys.argv) == 1:
        sys.argv.append("dev")
    main()

if __name__ == "__main__":
    main()