#!/usr/local/bin/python3.13
# -*- coding: utf-8 -*-
"""Reel builder per docs/reel-editing-flow.md (cloud adaptation).

spec JSON:
{
  "name": "out_name",
  "fps": 30, "w":1080, "h":1920,
  "segments": [
     {"src":"src/v1_beauty.mp4", "in":12.3, "out":18.9,
      "crop_x": null,            # center of crop in source px (null=center 960)
      "speed": 1.0}
  ],
  "hook": {"en":"...", "ja":"...", "dur":5.5},
  "cues": [ {"s":0.4,"e":3.1,"en":"...","ja":"..."} ],   # times in OUTPUT timeline
  "audio": "keep"   # or "silent"
}
"""
import json, os, subprocess, sys, math
from PIL import Image, ImageDraw, ImageFont

W,H = 1080,1920
FONT_B = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"    # W7相当
FONT_R = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc" # W6相当
YELLOW=(255,224,102); WHITE=(255,255,255); BLACK=(0,0,0)

def _fit(draw, text, font_path, size, max_w, stroke):
    while size > 18:
        f = ImageFont.truetype(font_path, size)
        if draw.textlength(text, font=f) <= max_w: return f, size
        size -= 2
    return ImageFont.truetype(font_path, size), size

def _wrap(draw, text, font_path, size, max_w, stroke):
    f = ImageFont.truetype(font_path, size)
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur+ch, font=f) > max_w and cur:
            lines.append(cur); cur = ch
        else:
            cur += ch
    # prefer breaking at spaces for latin text
    if " " in text and len(lines)>0:
        words=text.split(" "); lines,cur=[],""
        for wd in words:
            t=(cur+" "+wd).strip()
            if draw.textlength(t,font=f)>max_w and cur:
                lines.append(cur); cur=wd
            else: cur=t
    lines.append(cur)
    return lines, f

def cue_png(en, ja, path):
    """Bilingual cue: JA (yellow 46px) above EN (white 60px), block bottom y=1560."""
    img = Image.new("RGBA",(W,H),(0,0,0,0)); d = ImageDraw.Draw(img)
    max_w = W-60
    blocks=[]  # (lines, font, color, stroke, line_h)
    if en:
        lines,f=_wrap(d,en,FONT_B,64,max_w,8)
        blocks.append((lines,f,WHITE,8,82))
    if ja:
        lines,f=_wrap(d,ja,FONT_R,40,max_w,5)
        blocks.insert(0,(lines,f,YELLOW,5,52))
    total=sum(len(l)*lh for l,_,_,_,lh in blocks)+ (10 if len(blocks)>1 else 0)
    y = 1560-total
    for lines,f,col,st,lh in blocks:
        for ln in lines:
            w=d.textlength(ln,font=f)
            d.text(((W-w)//2,y),ln,font=f,fill=col,stroke_width=st,stroke_fill=BLACK)
            y+=lh
        y+=10
    img.save(path)


def toplabel_png(en, ja, path):
    img=Image.new("RGBA",(W,H),(0,0,0,0)); d=ImageDraw.Draw(img)
    d.rectangle([0,0,1080,470 if ja else 400],fill=(10,10,10,255))
    f=ImageFont.truetype(FONT_B,56); w=d.textlength(en,font=f)
    d.text(((W-w)//2,170),en,font=f,fill=WHITE)
    if ja:
        f2=ImageFont.truetype(FONT_R,38); w2=d.textlength(ja,font=f2)
        d.text(((W-w2)//2,300),ja,font=f2,fill=YELLOW)
    img.save(path)

def hook_png(en, ja, path, loc=None):
    """Hook: EN 64px bold white + JA 44px yellow, top area y=220."""
    img=Image.new("RGBA",(W,H),(0,0,0,0)); d=ImageDraw.Draw(img)
    max_w=W-60; y=220
    if en:
        lines,f=_wrap(d,en,FONT_B,64,max_w,7)
        for ln in lines:
            w=d.textlength(ln,font=f)
            d.text(((W-w)//2,y),ln,font=f,fill=WHITE,stroke_width=7,stroke_fill=BLACK); y+=80
    if ja:
        y+=8
        lines,f=_wrap(d,ja,FONT_R,44,max_w,5)
        for ln in lines:
            w=d.textlength(ln,font=f)
            d.text(((W-w)//2,y),ln,font=f,fill=YELLOW,stroke_width=5,stroke_fill=BLACK); y+=56
    if loc:
        y+=18
        f=ImageFont.truetype(FONT_R,40)
        w=d.textlength(loc,font=f)
        d.text(((W-w)//2,y),loc,font=f,fill=(235,235,235),stroke_width=4,stroke_fill=BLACK)
    img.save(path)

def run(c): subprocess.run(c,check=True)

def build(spec_path):
    spec=json.load(open(spec_path)); os.chdir(os.path.dirname(os.path.abspath(spec_path)))
    name=spec["name"]; os.makedirs("work",exist_ok=True); os.makedirs("out",exist_ok=True)
    fps=spec.get("fps",30)
    # 1) cut + crop segments to 1080x1920
    seglist=[]
    for i,s in enumerate(spec["segments"]):
        seg=f"work/{name}_seg{i}.mp4"
        if s.get("crop"):  # explicit [w,h,x,y] zoom crop
            cw,ch,xx,yy=s["crop"]
            vf=f"crop={cw}:{ch}:{xx}:{yy},scale=1080:1920:flags=lanczos"
        else:
            cx=s.get("crop_x"); cx=960 if cx is None else cx
            x=int(max(0,min(1920-608,cx-304)))
            vf=f"crop=608:1080:{x}:0,scale=1080:1920:flags=lanczos"
        sp=s.get("speed",1.0)
        if sp!=1.0: vf+=f",setpts=PTS/{sp}"
        af=f"atempo={sp}" if sp!=1.0 else "anull"
        if s.get("mute"): af="volume=0"
        dur=round(s["out"]-s["in"],3)
        if s.get("vsrc"):  # video from vsrc/vin, audio from src/in
            run(["ffmpeg","-nostdin","-v","error","-y",
                 "-ss",str(s["vin"]),"-t",str(dur),"-i",s["vsrc"],
                 "-ss",str(s["in"]),"-t",str(dur),"-i",s["src"],
                 "-map","0:v","-map","1:a","-vf",vf,"-r",str(fps),
                 "-af",af,"-c:v","libx264","-preset","veryfast","-crf","19",
                 "-c:a","aac","-b:a","160k","-ar","48000","-ac","2",seg])
        else:
            run(["ffmpeg","-nostdin","-v","error","-y","-ss",str(s["in"]),"-t",str(dur),
                 "-i",s["src"],"-vf",vf,"-r",str(fps),
                 "-af",af,"-c:v","libx264","-preset","veryfast","-crf","19",
                 "-c:a","aac","-b:a","160k","-ar","48000","-ac","2",seg])
        seglist.append(seg)
    # 2) concat
    with open(f"work/{name}_list.txt","w") as f:
        for s in seglist: f.write(f"file '{os.path.abspath(s)}'\n")
    base=f"work/{name}_base.mp4"
    run(["ffmpeg","-nostdin","-v","error","-y","-f","concat","-safe","0","-i",f"work/{name}_list.txt",
         "-c:v","libx264","-preset","veryfast","-crf","19","-c:a","aac","-b:a","160k",base])
    # 3) overlays: hook + cues
    inputs=["-i",base]; filters=[]; last="[0:v]"; idx=1
    if spec.get("hook"):
        hp=f"work/{name}_hook.png"; hook_png(spec["hook"].get("en"),spec["hook"].get("ja"),hp,spec["hook"].get("loc"))
        inputs+=["-i",hp]
        filters.append(f"{last}[{idx}:v]overlay=0:0:enable='between(t,0,{spec['hook'].get('dur',5.5)})'[v{idx}]")
        last=f"[v{idx}]"; idx+=1
    for j,c in enumerate(spec.get("cues",[])):
        cp=f"work/{name}_cue{j}.png"; cue_png(c.get("en"),c.get("ja"),cp)
        inputs+=["-i",cp]
        filters.append(f"{last}[{idx}:v]overlay=0:0:enable='between(t,{c['s']},{c['e']})'[v{idx}]")
        last=f"[v{idx}]"; idx+=1
    for j,c in enumerate(spec.get("top_labels",[])):
        tp=f"work/{name}_top{j}.png"; toplabel_png(c["en"],c.get("ja"),tp)
        inputs+=["-i",tp]
        filters.append(f"{last}[{idx}:v]overlay=0:0:enable='between(t,{c['s']},{c['e']})'[v{idx}]")
        last=f"[v{idx}]"; idx+=1
    out=f"out/{name}.mp4"
    if spec.get("audio")=="silent":
        amap=["-an"]  # replaced below with anullsrc
    fc=";".join(filters) if filters else None
    cmd=["ffmpeg","-nostdin","-v","error","-y"]+inputs
    if spec.get("audio")=="silent":
        cmd+=["-f","lavfi","-i","anullsrc=r=48000:cl=stereo"]
    if fc: cmd+=["-filter_complex",fc,"-map",last]
    else: cmd+=["-map","0:v"]
    if spec.get("audio")=="silent":
        cmd+=["-map",f"{idx+ (1 if False else 0)}:a" if False else f"{len(inputs)//2}:a"]
    else:
        cmd+=["-map","0:a"]
    cmd+=["-c:v","libx264","-preset","medium","-crf","18","-pix_fmt","yuv420p",
          "-c:a","aac","-b:a","160k","-ar","48000","-movflags","+faststart"]
    if spec.get("audio")=="silent": cmd+=["-shortest"]
    cmd+=[out]
    run(cmd)
    fo=spec.get("fade_out")
    if fo:
        import subprocess as sp
        total=float(sp.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",out],capture_output=True,text=True).stdout.strip())
        st=round(total-fo,2)
        tmp=out.replace(".mp4","_fade.mp4")
        run(["ffmpeg","-nostdin","-v","error","-y","-i",out,
             "-vf",f"fade=t=out:st={st}:d={fo}","-af",f"afade=t=out:st={st}:d={fo}",
             "-c:v","libx264","-preset","medium","-crf","18","-pix_fmt","yuv420p",
             "-c:a","aac","-b:a","160k","-movflags","+faststart",tmp])
        os.replace(tmp,out)
    mb=spec.get("music_bed")
    if mb:
        import subprocess as sp
        total=float(sp.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",out],capture_output=True,text=True).stdout.strip())
        hi=mb.get("v_hi",0.55); lo=mb.get("v_lo",0.12); da=mb.get("duck_at",3.0); dr=mb.get("duck_len",1.2)
        fst=round(total-1.6,2)
        vol=f"if(lt(t,{da}),{hi},if(lt(t,{da+dr}),{hi}-({hi}-{lo})/{dr}*(t-{da}),{lo}))"
        tmp=out.replace(".mp4","_mix.mp4")
        bed=f"work/{name}_bed.wav"
        if mb.get("intro_in") is not None:
            sw=f"work/{name}_sw.wav"; am=f"work/{name}_am.wav"
            il=mb.get("intro_len",3.4); al=mb.get("len",10.0)
            run(["ffmpeg","-nostdin","-v","error","-y","-ss",str(mb["intro_in"]),"-t",str(il),
                 "-i",mb["src"],"-vn","-c:a","pcm_s16le","-ar","48000","-ac","2",sw])
            run(["ffmpeg","-nostdin","-v","error","-y","-ss",str(mb["in"]),"-t",str(al),
                 "-i",mb["src"],"-vn","-c:a","pcm_s16le","-ar","48000","-ac","2",am])
            rep=int((total-il)/al)+2
            ins=["-i",sw]+["-i",am]*rep
            streams="".join(f"[{k}:a]" for k in range(rep+1))
            run(["ffmpeg","-nostdin","-v","error","-y"]+ins+["-filter_complex",
                 f"{streams}concat=n={rep+1}:v=0:a=1[b]","-map","[b]","-t",str(total),
                 "-c:a","pcm_s16le",bed])
        else:
            run(["ffmpeg","-nostdin","-v","error","-y","-ss",str(mb["in"]),"-t",str(mb.get("len",12.0)),
                 "-i",mb["src"],"-vn","-c:a","pcm_s16le","-ar","48000","-ac","2",bed])
        run(["ffmpeg","-nostdin","-v","error","-y","-i",out,
             "-stream_loop","-1","-t",str(total),"-i",bed,
             "-filter_complex",
             f"[1:a]volume='{vol}':eval=frame,afade=t=out:st={fst}:d=1.6[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
             "-map","0:v","-map","[a]","-c:v","copy",
             "-c:a","aac","-b:a","160k","-ar","48000","-movflags","+faststart",tmp])
        os.replace(tmp,out)
    print("BUILT",out)

if __name__=="__main__":
    build(sys.argv[1])
