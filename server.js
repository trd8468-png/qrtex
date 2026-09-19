const express=require("express");
const path=require("path");
const app=express();
app.use(express.json({limit:"20kb"}));
app.use(express.static(path.join(__dirname,"public")));

const PORT=process.env.PORT||10000;
const TOKEN=process.env.GITHUB_TOKEN;
const REPO=process.env.GITHUB_REPO||"trd8468-png/qrtex";
const BRANCH=process.env.GITHUB_BRANCH||"main";

function apiUrl(file="notes.json"){return "https://api.github.com/repos/"+REPO+"/contents/"+file+"?ref="+encodeURIComponent(BRANCH)}
async function gh(pathname,opts={}){
  if(!TOKEN) throw new Error("GITHUB_TOKEN is not configured");
  const r=await fetch(pathname,{...opts,headers:{"Accept":"application/vnd.github+json","Authorization":"Bearer "+TOKEN,"X-GitHub-Api-Version":"2022-11-28",...(opts.headers||{})}});
  const text=await r.text();let data;try{data=JSON.parse(text)}catch{data={message:text}}
  if(!r.ok) throw new Error(data.message||"GitHub API error");
  return data;
}
async function readNotes(){
  const data=await gh(apiUrl());
  const raw=Buffer.from(data.content.replace(/\n/g,""),"base64").toString("utf8");
  return {notes:JSON.parse(raw),sha:data.sha};
}
app.get("/api/notes/:id",async(req,res)=>{
  try{
    const {notes}=await readNotes();
    const id=String(req.params.id).toLowerCase();
    if(!notes[id]) return res.status(404).json({error:"Note not found"});
    res.set("Cache-Control","no-store");
    res.json(notes[id]);
  }catch(e){res.status(500).json({error:"Storage unavailable"});}
});
app.post("/api/notes",async(req,res)=>{
  try{
    const id=String(req.body.id||"").trim().toLowerCase();
    const title=String(req.body.title||"").trim().slice(0,160);
    const text=String(req.body.text||"").trim().slice(0,10000);
    if(!/^[a-z0-9_-]{2,80}$/.test(id)) return res.status(400).json({error:"Use 2-80 letters, numbers, hyphens or underscores for the Note ID."});
    if(!text) return res.status(400).json({error:"Text is required."});
    const current=await readNotes();
    if(current.notes[id]) return res.status(409).json({error:"That permanent Note ID already exists. Choose another."});
    current.notes[id]={title:title||"Permanent Note",text};
    const content=Buffer.from(JSON.stringify(current.notes,null,2)+"\n").toString("base64");
    await gh("https://api.github.com/repos/"+REPO+"/contents/notes.json",{
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({message:"Create permanent note: "+id,content,sha:current.sha,branch:BRANCH})
    });
    res.status(201).json({id});
  }catch(e){res.status(500).json({error:e.message||"Could not create note"});}
});
app.use((req,res)=>res.sendFile(path.join(__dirname,"public","index.html")));
app.listen(PORT,()=>console.log("QRTex running on "+PORT));