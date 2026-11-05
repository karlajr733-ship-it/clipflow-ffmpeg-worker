import os, re, hmac, hashlib, time, tempfile, subprocess, json, asyncio, ipaddress, socket
from pathlib import Path
from urllib.parse import urlencode, urlparse
import httpx
import yt_dlp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app=FastAPI(title="ClipFlow FFmpeg Worker")
SECRET=os.environ.get("CLIPFLOW_WORKER_SECRET","")
PUBLIC=os.environ.get("PUBLIC_BASE_URL","").rstrip("/")
TEMP=Path("/tmp/clipflow")
TEMP.mkdir(exist_ok=True)
MAX_BYTES=1_000_000_000
def auth(request:Request):
    if not SECRET or not hmac.compare_digest(request.headers.get("x-clipflow-secret",""),SECRET):
        raise HTTPException(401,"Unauthorized")
def safe_url(url:str):
    p=urlparse(url)
    if p.scheme!="https" or not p.hostname:
        raise HTTPException(400,"HTTPS URL required")
    # Only the server-side Floot app may call this API; block local/private hosts.
    try:
        addresses=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise HTTPException(400,"Private source host rejected")
    except socket.gaierror:
        raise HTTPException(400,"Unresolvable host")
    return url
async def download(url:str,path:Path):
    
    if urlparse(url).hostname in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        api_key = os.environ.get("VIDEOFETCH_API_KEY", "")
        if not api_key:
            raise HTTPException(503, "VidFetch API key missing")

        headers = {"Authorization": f"Bearer {api_key}"}

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.vidfetch.dev/v1/downloads",
                headers=headers,
                json={
                    "url": url,
                    "format": "720p",
                    "destination": "url"
                }
            )
            if response.status_code >= 400:
                raise HTTPException(502, f"VidFetch import request failed (HTTP {response.status_code})")

            job_id = response.json()["id"]
            video_url = None

            for attempt in range(400):
                await asyncio.sleep(3)
                result = await client.get(
                    f"https://api.vidfetch.dev/v1/downloads/{job_id}",
                    headers=headers
                )
                if result.status_code >= 400:
                    raise HTTPException(502, "VidFetch status check failed")

                job = result.json()
                if job["status"] == "completed":
                    video_url = job.get("download_url")
                    break
                if job["status"] == "failed":
                    raise HTTPException(422, "YouTube import failed")

        if not video_url:
            raise HTTPException(504, "YouTube import timed out")

        safe_url(video_url)
        url = video_url

    safe_url(url)
  
    async with httpx.AsyncClient(timeout=90,follow_redirects=False) as client:
        async with client.stream("GET",url) as response:
            response.raise_for_status()
            size=0
            with path.open("wb") as f:
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>MAX_BYTES:raise HTTPException(413,"File too large")
                    f.write(chunk)
def run(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=420)
def signed(job:str,exp:int):
    return hmac.new(SECRET.encode(),f"{job}:{exp}".encode(),hashlib.sha256).hexdigest()
def workdir(job):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}",job):raise HTTPException(400,"Invalid job id")
    d=TEMP/job;d.mkdir(parents=True,exist_ok=True);return d
@app.get("/health")
def health():
    return {"ok":bool(SECRET),"ffmpeg":"available"}
class Prepare(BaseModel):
    jobId:str
    sourceUrl:str
@app.post("/prepare")
async def prepare(request:Request,body:Prepare):
    auth(request)
    if not PUBLIC.startswith("https://"):raise HTTPException(503,"PUBLIC_BASE_URL must be the public HTTPS Railway origin")
    folder=workdir(body.jobId)
    source=folder/"source"
    await download(body.sourceUrl,source)
    metadata=json.loads(run("ffprobe","-v","quiet","-show_format","-of","json",str(source)).stdout)
    duration=float(metadata["format"]["duration"])
    if not 5<=duration<=3600:raise HTTPException(400,"Duration must be 5 sec to 30 min")
    audio=folder/"audio.mp3"
    run("ffmpeg","-y","-i",str(source),"-vn","-ac","1","-ar","16000","-b:a","64k",str(audio))
    exp=int(time.time())+3600
    return {"duration":duration,"audioUrl":f"{PUBLIC}/audio/{body.jobId}?"+urlencode({"exp":exp,"sig":signed(body.jobId,exp)})}
@app.get("/audio/{job}")
async def audio(job:str,exp:int,sig:str):
    folder=workdir(job)
    if exp<int(time.time()) or exp>int(time.time())+3600 or not hmac.compare_digest(sig,signed(job,exp)):
        raise HTTPException(403,"Expired or invalid audio link")
    path=folder/"audio.mp3"
    if not path.exists():raise HTTPException(404,"Audio missing")
    return FileResponse(path,media_type="audio/mpeg")
class Word(BaseModel):
    text:str
    start:float
    end:float
class Clip(BaseModel):
    id:str
    title:str
    start:float
    end:float
    words:list[Word]=Field(default_factory=list)
class Render(BaseModel):
    jobId:str
    sourceUrl:str
    callbackOrigin:str
    clips:list[Clip]
def ass_time(t):
    h=int(t//3600);m=int((t%3600)//60);s=t%60
    return f"{h}:{m:02d}:{s:05.2f}"
def ass_escape(t):
    return t.replace("\\","").replace("{","").replace("}","").replace("\n"," ")
def subtitles(path:Path,clip:Clip):
    lines=["[Script Info]","ScriptType: v4.00+","PlayResX: 1080","PlayResY: 1920",
           "[V4+ Styles]","Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
           "Style: Default,DejaVu Sans,74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,60,60,240,1",
           "[Events]","Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    words=[w for w in clip.words if w.end>clip.start and w.start<clip.end]
    for i in range(0,len(words),4):
        group=words[i:i+4]
        start=max(0,group[0].start-clip.start)
        end=min(clip.end-clip.start,group[-1].end-clip.start)
        if end<=start:continue
        text=" ".join(ass_escape(w.text) for w in group)
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    path.write_text("\n".join(lines),encoding="utf-8")
@app.post("/render")
async def render(request:Request,body:Render):
    auth(request)
    if len(body.clips)>10 or not body.clips:raise HTTPException(400,"Invalid clip count")
    if not body.callbackOrigin.startswith("https://"):raise HTTPException(400,"HTTPS callback required")
    # Prevent arbitrary callback destinations. Configure the Floot deployment origin.
    allowed=os.environ.get("FLOOT_APP_ORIGIN","").rstrip("/")
    if not allowed or body.callbackOrigin.rstrip("/")!=allowed:
        raise HTTPException(403,"Callback origin not configured or does not match FLOOT_APP_ORIGIN")
    folder=workdir(body.jobId)
    source=folder/"source"
    if not source.exists():await download(body.sourceUrl,source)
    done=[]
    async with httpx.AsyncClient(timeout=120) as client:
        for c in body.clips:
            if not re.fullmatch(r"[0-9a-fA-F-]{36}",c.id) or not (0<=c.start<c.end<=3600):
                raise HTTPException(400,"Invalid clip")
            output=folder/(c.id+".mp4")
            ass=folder/(c.id+".ass")
            subtitles(ass,c)
            # Center crop: a robust MVP fallback. Speaker-aware tracking requires another model.
            filtergraph=f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,subtitles={ass}"
            run("ffmpeg","-y","-ss",str(c.start),"-i",str(source),"-t",str(c.end-c.start),"-vf",filtergraph,
                "-c:v","libx264","-preset","veryfast","-crf","25","-pix_fmt","yuv420p","-c:a","aac","-b:a","128k",
                "-movflags","+faststart",str(output))
            size=output.stat().st_size
            if size>MAX_BYTES:raise HTTPException(413,"Rendered clip exceeds 100 MB")
            response=await client.post(body.callbackOrigin+"/_api/worker/presign",
                headers={"X-ClipFlow-Secret":SECRET},json={"jobId":body.jobId,"clipId":c.id,"sizeBytes":size})
            response.raise_for_status()
            signed=response.json()
            with output.open("rb") as f:
                uploaded=await client.put(signed["uploadUrl"],headers=signed["headers"],content=f.read())
                uploaded.raise_for_status()
            done.append(c.id)
            output.unlink(missing_ok=True)
    return {"completed":done}
