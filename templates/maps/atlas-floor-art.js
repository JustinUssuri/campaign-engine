/* 按既有尺数画俯视材质：几何仍由 floorplans.json 决定，文字和点击由 atlas.html 处理。
   arid-sandstone 使用项目内四格纹理；无外部网络、字体或滤镜，微纹按稳定种子生成。
   terrain 是纯绘画层，不会创建房间、路径状态或碰撞。 */
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const n = x => Math.round(x * 100) / 100;
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
  function hash(s) {
    let h = 2166136261;
    for (const c of String(s)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
    return h >>> 0;
  }
  function random(seed) {
    let h = hash(seed);
    return () => {
      h += 0x6D2B79F5;
      let t = h;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function points(r) {
    if (r.rect) {
      const [x0,y0,x1,y1] = r.rect;
      return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]];
    }
    return r.poly;
  }
  const terrainPoints = t => t.points || (t.rect || t.poly ? points(t) : []);
  const path = pts => 'M' + pts.map(p => p.map(n).join(' ')).join('L') + 'Z';
  const openPath = pts => 'M' + pts.map(p => p.map(n).join(' ')).join('L');
  const line = (x1,y1,x2,y2,stroke,width = .4,opacity = 1) =>
    `<path d="M${n(x1)} ${n(y1)}L${n(x2)} ${n(y2)}" fill="none" stroke="${stroke}" stroke-width="${width}" opacity="${opacity}" stroke-linecap="round"/>`;
  const rect = (x,y,w,h,fill,extra = '') => `<rect x="${n(x)}" y="${n(y)}" width="${n(w)}" height="${n(h)}" fill="${fill}" ${extra}/>`;
  const circle = (x,y,r,fill,opacity) => `<circle cx="${n(x)}" cy="${n(y)}" r="${n(r)}" fill="${fill}" opacity="${opacity}"/>`;
  function inside(p, pts) {
    let hit = false;
    for (let i = 0,j = pts.length - 1; i < pts.length; j = i++) {
      const a = pts[i], b = pts[j];
      if ((a[1] > p[1]) !== (b[1] > p[1]) && p[0] < (b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]) hit = !hit;
    }
    return hit;
  }
  function paletteDefs(prefix,palette) {
    const arid=palette==='arid-sandstone';
    const rand = random('fortress materials v1');
    const pat = (id,size,body) => `<pattern id="${prefix}-${id}" width="${size[0]}" height="${size[1]}" patternUnits="userSpaceOnUse">${body}</pattern>`;
    let earth = rect(0,0,40,40,'#b2a184');
    for (let i=0;i<105;i++) {
      const x=rand()*40,y=rand()*40,r=.12+rand()*.48;
      earth += circle(x,y,r, ['#e0cdb0','#8e826f','#c6b493','#796f5e'][i%4],.18+rand()*.2);
    }
    for (let i=0;i<13;i++) {
      const x=rand()*40,y=rand()*40;
      earth += `<path d="M${n(x)} ${n(y)}l.8 .15 .25 .7 -.8 .35 -.45 -.5Z" fill="#888374" opacity=".42"/>`;
    }
    let masonry = rect(0,0,18,10,'#514f47');
    const stoneColors = ['#898678','#a29c89','#918d7e','#787b70','#aaa18d','#999480'];
    for (let row=0;row<4;row++) {
      const offset=row%2 ? -3 : 0;
      for (let col=0;col<4;col++) {
        const x=offset+col*6,y=row*2.5;
        masonry += rect(x+.18,y+.16,5.65,2.16,stoneColors[(row*3+col)%stoneColors.length],'rx=".25"');
        masonry += line(x+.5,y+2.14,x+5.45,y+2.14,'#d0c5ad',.16,.48);
        masonry += line(x+5.6,y+.5,x+5.6,y+1.9,'#464a43',.16,.55);
      }
    }
    let stone = rect(0,0,32,24,'#99927f');
    const sc=['#b0a791','#b4ab94','#aba38f','#b3aa93','#aaa48f','#b1aa94'];
    for (let row=0;row<3;row++) {
      const offset=row%2?-5.3:0;
      for (let col=0;col<4;col++) {
        const x=offset+col*10.67,y=row*8;
        stone += `<path d="M${n(x+.3)} ${n(y+.3)}L${n(x+10.25)} ${n(y+.15)}L${n(x+10.4)} ${n(y+7.6)}L${n(x+.2)} ${n(y+7.8)}Z" fill="${sc[(row+col)%sc.length]}"/>`;
        stone += line(x+1.1,y+6.9,x+3.1,y+7.2,'#d6cdb8',.18,.5);
      }
    }
    stone += '<path d="M4 4l3 -.5 1 -2M21 17l-1 -3 2 -1M7 22l4 -1 2 1" fill="none" stroke="#716d62" stroke-width=".2" opacity=".55"/>';
    let wood=rect(0,0,16,24,'#8c7253');
    for (let row=0;row<6;row++) {
      const y=row*4;
      wood += rect(.12,y+.12,15.75,3.75,['#a58a63','#957d58','#ad9065','#9c835c','#a28a64','#927952'][row]);
      wood += line(0,y,16,y,'#554e3e',.25,.68);
      const joint=(row%3)*5+1;
      wood += line(joint,y+.1,joint,y+3.7,'#675844',.2,.55);
      for (let k=0;k<3;k++) wood += line(rand()*3,y+.9+k*.8,10+rand()*6,y+.8+k*.8,'#5d523f',.12,.18);
    }
    wood += '<ellipse cx="11" cy="10" rx="1.3" ry=".4" fill="none" stroke="#66513c" stroke-width=".15" opacity=".42"/>';
    let straw=earth;
    for (let i=0;i<70;i++) {
      const x=rand()*40,y=rand()*40,dx=.7+rand()*1.4,dy=(rand()-.5)*2.1;
      straw += line(x,y,x+dx,y+dy,i%3?'#c6ad73':'#e0ca91',.17,.7);
    }
    let rubble=stone;
    for(let i=0;i<12;i++) {
      const x=rand()*32,y=rand()*24;
      rubble += `<path d="M${n(x)} ${n(y)}l1.6 .3 .7 1.2 -1.4 .8 -1.2 -.6Z" fill="#8b887a" stroke="#66675e" stroke-width=".12" opacity=".65"/>`;
    }
    let ruin=rect(0,0,30,24,'#aaa18b');
    for(let i=0;i<23;i++) {
      const x=rand()*30,y=rand()*24,w=.4+rand()*1.1,h=.3+rand()*.7;
      ruin+=`<path d="M${n(x)} ${n(y)}l${n(w)} .15 .2 ${n(h)} ${n(-w*.8)} .25 -.35 -.3Z" fill="${['#8d9080','#b7ad95','#a5a18c'][i%3]}" stroke="#7f8373" stroke-width=".1" opacity=".7"/>`;
      if(i%6===0) ruin+=line(x,y,x+.3,y+1.15,'#7d8162',.15,.45);
    }
    ruin+='<path d="M2 6l5 2 2 -1 4 3M19 18l-2 -3 1 -3 5 -1" fill="none" stroke="#827f6d" stroke-width=".2" opacity=".35"/>';
    let water=rect(0,0,24,24,'#8b9187');
    for(let i=0;i<12;i++) {
      const x=rand()*24,y=rand()*24;
      water+=line(x,y,x+1.5+rand()*2,y+.1,'#b6b8a1',.14,.3);
    }
    // Crop by SVG viewBox, not by modifying the original generated bitmap.
    // One same-origin resource serves all four material samples and is cached.
    const photo=(id,q,size,color,opacity)=>{
      const qx=q%2*627,qy=Math.floor(q/2)*627;
      return `<pattern id="${prefix}-${id}" width="${size}" height="${size}" patternUnits="userSpaceOnUse" viewBox="${qx} ${qy} 627 627" preserveAspectRatio="none" overflow="hidden">`+
        rect(qx,qy,627,627,color)+`<image href="/maps/img/fort-arid-materials.png" x="0" y="0" width="1254" height="1254" opacity="${opacity}"/>`+
        rect(qx,qy,627,627,'#cfb38c','opacity=".12"')+'</pattern>';
    };
    let photoDefs='';
    if(arid) {
      photoDefs=photo('photo-masonry',0,16,'#a8765c',.94)+photo('photo-plaster',1,28,'#bc8d70',.78)+photo('photo-earth',2,40,'#ac8468',.72)+photo('photo-rock',3,144,'#b58565',.74);
      earth=rect(0,0,40,40,`url(#${prefix}-photo-earth)`);
      straw=earth;
      for(let i=0;i<55;i++) {
        const x=rand()*40,y=rand()*40;
        straw+=line(x,y,x+.7+rand()*1.8,y+(rand()-.5)*2,'#c7b07c',.16,.58);
      }
      masonry=rect(0,0,18,10,`url(#${prefix}-photo-masonry)`)+rect(0,0,18,10,'#bb9473','opacity=".09"');
      // Keep laid-stone joints legible without returning to grey European paving.
      stone=rect(0,0,32,24,`url(#${prefix}-photo-plaster)`)+`<g opacity=".22">${stone}</g>`;
      ruin=rect(0,0,30,24,`url(#${prefix}-photo-earth)`)+`<g opacity=".42">${ruin}</g>`;
      rubble=rect(0,0,32,24,`url(#${prefix}-photo-rock)`)+`<g opacity=".34">${rubble}</g>`;
    }
    const rockSample=rect(0,0,144,144,`url(#${prefix}-photo-rock)`);
    // A 144-foot sample is mirrored into a 288-foot repeat. Adjacent edges
    // meet the same image edge, avoiding the former 46-foot grid seams.
    const rock=arid?rockSample+`<g transform="translate(288,0) scale(-1,1)">${rockSample}</g><g transform="translate(0,288) scale(1,-1)">${rockSample}</g><g transform="translate(288,288) scale(-1,-1)">${rockSample}</g>`:rect(0,0,46,46,'#a39c86');
    return `<defs>${photoDefs}${pat('earth',[40,40],earth)}${pat('masonry',[9,5],'<g transform="scale(.5)">'+masonry+'</g>')}${pat('stone',[8,6],'<g transform="scale(.25)">'+stone+'</g>')}${pat('wood',[16,6],'<g transform="scale(1,.25)">'+wood+'</g>')}${pat('straw',[40,40],straw)}${pat('rubble',[8,6],'<g transform="scale(.25)">'+rubble+'</g>')}${pat('ruin',[30,24],ruin)}${pat('water',[24,24],water)}${pat('plaster',[28,28],arid?rect(0,0,28,28,`url(#${prefix}-photo-plaster)`):rect(0,0,28,28,'#c1b69b'))}${pat('rock',arid?[288,288]:[46,46],rock)}
      <linearGradient id="${prefix}-wall-light" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#eee1c6" stop-opacity=".24"/><stop offset=".55" stop-color="#c5b99d" stop-opacity="0"/><stop offset="1" stop-color="#282d29" stop-opacity=".25"/></linearGradient>
      <linearGradient id="${prefix}-step-light" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4b4d43"/><stop offset="1" stop-color="#c0b49a"/></linearGradient>
    </defs>`;
  }
  function inferMaterial(r,f) {
    if(r.material) return r.material;
    if(/墙/.test(r.name)) return 'masonry';
    if(/封堵/.test(r.name)) return 'wood';
    if(/祭坛|石龛/.test(r.name)) return 'rubble';
    if(/马厩/.test(r.name)) return 'straw';
    if(/长屋|私室|储草/.test(r.name)) return 'wood';
    if(r.kind==='street' || r.name==='外院') return 'earth';
    if(f.kind==='cellar' || /内堡|地窖|石室|门|过道/.test(r.name)) return 'stone';
    return r.kind==='yard'?'earth':'stone';
  }
  function shadow(pts) {
    const d=path(pts);
    return `<path d="${d}" fill="#302f27" opacity=".08" transform="translate(2.3,-2.3)"/>`+
      `<path d="${d}" fill="#302f27" opacity=".14" transform="translate(1.1,-1.1)"/>`;
  }
  function isWall(r) { return r.material ? r.material==='masonry' : r.kind==='fixture' && /墙/.test(r.name); }
  function wall(r,prefix) {
    const d=path(points(r));
    const arid=prefix.endsWith('-arid');
    const edge=arid?`<path d="${d}" fill="none" stroke="#e4c094" stroke-width=".35" opacity=".48"/>`:'';
    return shadow(points(r))+`<path d="${d}" fill="url(#${prefix}-masonry)" stroke="${arid?'#5d3828':'#45493f'}" stroke-width="${arid?.95:.6}" stroke-linejoin="round"/>`+
      `<path d="${d}" fill="url(#${prefix}-wall-light)" stroke="#d0c6ad" stroke-width=".22" stroke-linejoin="round"/>`+edge;
  }
  function doorsForRoom(r,doors) {
    if(!r.rect) return [];
    const [x0,y0,x1,y1]=r.rect, eps=.01;
    return doors.filter(d => d.length===2 && (
      (Math.abs(d[0][0]-d[1][0])<eps && (Math.abs(d[0][0]-x0)<eps || Math.abs(d[0][0]-x1)<eps) && Math.min(d[0][1],d[1][1])>=y0-eps && Math.max(d[0][1],d[1][1])<=y1+eps) ||
      (Math.abs(d[0][1]-d[1][1])<eps && (Math.abs(d[0][1]-y0)<eps || Math.abs(d[0][1]-y1)<eps) && Math.min(d[0][0],d[1][0])>=x0-eps && Math.max(d[0][0],d[1][0])<=x1+eps)));
  }
  function doorCut(r,d,wallWidth,prefix,material) {
    const [x0,y0,x1,y1]=r.rect, [[ax,ay],[bx,by]]=d;
    const w=wallWidth+.35, fill=`url(#${prefix}-${material})`;
    if(Math.abs(ax-bx)<.01) {
      const x=Math.abs(ax-x0)<.01?x0-.18:x1-w+.18;
      return rect(x,Math.min(ay,by),w,Math.abs(by-ay),fill)+line(ax,Math.min(ay,by)+.35,ax,Math.max(ay,by)-.35,'#63543d',.45,.8);
    }
    const y=Math.abs(ay-y0)<.01?y0-.18:y1-w+.18;
    return rect(Math.min(ax,bx),y,Math.abs(bx-ax),w,fill)+line(Math.min(ax,bx)+.35,ay,Math.max(ax,bx)-.35,ay,'#63543d',.45,.8);
  }
  function building(r,f,prefix) {
    const [x0,y0,x1,y1]=r.rect, w=r.wall_ft ?? (/西塔/.test(r.name)?4:2), material=inferMaterial(r,f);
    let s=wall(r,prefix)+rect(x0+w,y0+w,x1-x0-2*w,y1-y0-2*w,`url(#${prefix}-${material})`);
    // A narrow inner edge reads as a wall face, rather than a single diagram line.
    s += rect(x0+w,y0+w,x1-x0-2*w,y1-y0-2*w,'none',`stroke="#3e433a" stroke-width=".45"`);
    for(const d of doorsForRoom(r,f.doors||[])) s+=doorCut(r,d,w,prefix,material);
    if(r.use==='current') s+=rect(x0+w+.2,y0+w+.2,x1-x0-2*w-.4,y1-y0-2*w-.4,'none','stroke="#c3a778" stroke-width=".28" opacity=".64"');
    return s;
  }
  function ruinedBuilding(r,f,prefix) {
    const pts=points(r),d=path(pts),rand=random('ruin '+(r.id||r.name));
    const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
    const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
    const wallStroke=prefix.endsWith('-arid')?'#76513a':'#686e5e',wallEdgeWidth=prefix.endsWith('-arid')?.7:.35;
    let s=`<g data-condition="ruin" data-room-id="${esc(r.id||r.name)}">`;
    if(r.material==='water') {
      // Cistern rectangles record the clear basin, not an outside wall frame.
      // Paint the entire recorded net area; the fine outline does not subtract
      // an invented 1.5-foot wall from a 24-foot basin.
      const iw=x1-x0,ih=y1-y0;
      s+=`<path d="${d}" fill="#969d8e" stroke="#737e6a" stroke-width=".3"/>`;
      s+=`<path d="${d}" fill="url(#${prefix}-ruin)" opacity=".48"/>`;
      s+=`<path d="M${n(x0+iw*.08)} ${n(y0+ih*.22)}Q${n(x0+iw*.3)} ${n(y0+ih*.08)} ${n(x0+iw*.45)} ${n(y0+ih*.3)}T${n(x0+iw*.83)} ${n(y0+ih*.42)}L${n(x0+iw*.71)} ${n(y0+ih*.7)}Q${n(x0+iw*.38)} ${n(y0+ih*.55)} ${n(x0+iw*.15)} ${n(y0+ih*.69)}Z" fill="#71877e" opacity=".32"/>`;
      for(let i=0;i<5;i++) {
        const x=x0+iw*(.12+rand()*.65),y=y0+ih*(.12+rand()*.7);
        const sw=Math.min(4,iw*.18),sh=Math.min(2.6,ih*.16);
        s+=`<path d="M${n(x)} ${n(y)}l${n(sw)} .4 ${n(-sw*.15)} ${n(sh)} ${n(-sw*.85)} -.2Z" fill="#a5aa96" stroke="#737e6a" stroke-width=".18"/>`;
      }
      return s+'</g>';
    }
    if(r.kind==='fixture' && r.material==='masonry') {
      // This is a solid residual curtain-wall footprint, not a tiny building.
      s+=`<path d="${d}" transform="translate(.4,-.4)" fill="#44483c" opacity=".1"/>`;
      s+=`<path d="${d}" fill="url(#${prefix}-masonry)" stroke="${wallStroke}" stroke-width="${wallEdgeWidth}"/>`;
      s+=`<path d="${d}" fill="#c4bba4" opacity=".22"/>`;
      if(r.rect) for(let i=0;i<10;i++) {
        const vertical=y1-y0>x1-x0,fw=x1-x0,fh=y1-y0;
        const x=x0+fw*(.22+rand()*.5),y=y0+fh*(.05+rand()*.88);
        const ex=vertical?Math.min(x1-fw*.15,x+fw*.12):Math.min(x1-fw*.03,x+1+rand()*2);
        const ey=vertical?Math.min(y1-fh*.02,y+1+rand()*2):Math.min(y1-fh*.15,y+fh*.12);
        s+=line(x,y,ex,ey,'#d0c6ac',.25,.6);
      }
      return s+'</g>';
    }
    if(r.kind==='fixture' && r.material==='earth') {
      // An earth-banked midden gains no masonry lining or collapsed timber.
      s+=`<path d="${d}" fill="url(#${prefix}-earth)" stroke="#8c8066" stroke-width=".6"/>`;
      s+=`<path d="${d}" fill="#8f846a" opacity=".1"/>`;
      if(r.rect) s+=rect(x0+.45,y0+.45,x1-x0-.9,y1-y0-.9,'none','stroke="#b8a788" stroke-width=".4" opacity=".7"');
      return s+'</g>';
    }
    const w=r.wall_ft??1.5, inner=[x0+w,y0+w,x1-w,y1-w];
    // A continuous low wall base retains the exact recorded outline. Texture
    // erosion never cuts an unregistered opening through a defensive wall.
    s+=`<path d="${d}" transform="translate(.5,-.5)" fill="#44483c" opacity=".12"/>`;
    s+=`<path d="${d}" fill="url(#${prefix}-masonry)" stroke="${wallStroke}" stroke-width="${wallEdgeWidth}"/>`;
    s+=`<path d="${d}" fill="#c4bba4" opacity=".2"/>`;
    if(r.rect && inner[2]>inner[0] && inner[3]>inner[1]) {
      s+=rect(inner[0],inner[1],inner[2]-inner[0],inner[3]-inner[1],`url(#${prefix}-ruin)`,'stroke="#747a68" stroke-width=".28"');
      for(let i=0;i<11;i++) {
        const x=inner[0]+rand()*(inner[2]-inner[0]),y=inner[1]+rand()*(inner[3]-inner[1]);
        const sw=Math.min(1.5+rand()*2.4,Math.max(.2,inner[2]-x)),sh=Math.min(.5+rand()*1.4,Math.max(.2,inner[3]-y));
        s+=`<path d="M${n(x)} ${n(y)}l${n(sw)} .2 ${n(-sw*.12)} ${n(sh)} ${n(-sw*.84)} -.1Z" fill="${i%3?'#a3a48e':'#818a77'}" stroke="#717a66" stroke-width=".15"/>`;
      }
      // A few collapsed timbers remain wholly within the old floor footprint.
      for(let i=0;i<3;i++) {
        const iw=inner[2]-inner[0],ih=inner[3]-inner[1];
        const x=inner[0]+iw*(.18+rand()*.45),y=inner[1]+ih*(.2+rand()*.5);
        const len=Math.min(5+rand()*4,iw*.25),dy=Math.min(1+rand()*2,ih*.12);
        s+=line(x,y,x+len,y+dy,'#786b50',.6,.55)+line(x+.1,y+.2,x+len-.3,y+dy,'#b0a080',.16,.5);
      }
      // Pale chips stay inside the wall band; the foundation remains continuous.
      for(let i=0;i<10;i++) {
        const x=x0+w*.22+rand()*Math.max(.1,x1-x0-w*.6),y=i%2?y0+w*.45:y1-w*.55;
        s+=line(x,y,Math.min(x1-w*.2,x+1.5+rand()*2.3),y+(rand()-.5)*w*.2,'#d0c6ac',.28,.6);
      }
      for(const door of doorsForRoom(r,f.doors||[])) s+=doorCut(r,door,w,prefix,'ruin');
    } else {
      s+=`<path d="${d}" fill="url(#${prefix}-ruin)" opacity=".45"/>`;
    }
    return s+'</g>';
  }
  function rubbleAltar(r,prefix) {
    const pts=points(r),xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
    const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys),rand=random(r.id||r.name);
    let s=shadow(pts)+`<path d="${path(pts)}" fill="url(#${prefix}-rubble)" stroke="#6d6b5e" stroke-width=".35"/>`;
    for(let i=0;i<24;i++) {
      const x=x0+1+rand()*(x1-x0-3),y=y0+1+rand()*(y1-y0-3),w=1+rand()*2.7,h=.8+rand()*2;
      s+=`<path d="M${n(x)} ${n(y)}l${n(w)} .2 ${n(w*.12)} ${n(h)} ${n(-w*.95)} .3Z" fill="${['#a6a08b','#858879','#bdb39a','#757c70'][i%4]}" stroke="#555e52" stroke-width=".17"/>`;
    }
    return s;
  }
  function stairs(r,prefix) {
    if(!r.rect) return '';
    const [x0,y0,x1,y1]=r.rect,w=x1-x0,h=y1-y0;
    const vertical=h>=w, count=/西塔楼梯|楼梯口/.test(r.name)?15:Math.max(4,Math.round((vertical?h:w)/1.05));
    let s=rect(x0,y0,w,h,'#807c69',`stroke="#414a40" stroke-width=".35"`);
    for(let i=0;i<count;i++) {
      if(vertical) {
        const y=y0+i*h/count;
        s+=rect(x0+.3,y+.07,w-.6,h/count-.14,`url(#${prefix}-step-light)`);
      } else {
        const x=x0+i*w/count;
        s+=rect(x+.07,y0+.3,w/count-.14,h-.6,`url(#${prefix}-step-light)`);
      }
    }
    const cx=(x0+x1)/2,cy=(y0+y1)/2;
    const north=/西塔楼梯|楼梯口/.test(r.name), south=/石阶/.test(r.name);
    if(vertical && (north || south)) {
      const sign=north?1:-1, tip=cy+sign*h*.28, tail=cy-sign*h*.25;
      s+=line(cx,tail,cx,tip,'#e2d8b8',.32,.95);
      s+=`<path d="M${n(cx-.85)} ${n(tip-sign*1.2)}L${n(cx)} ${n(tip)}L${n(cx+.85)} ${n(tip-sign*1.2)}" fill="none" stroke="#e2d8b8" stroke-width=".32"/>`;
    }
    return s;
  }
  function stable(r,prefix) {
    if(!r.rect) return '';
    const [x0,y0,x1,y1]=r.rect,wallWidth=r.wall_ft??2;
    const ix0=x0+wallWidth,ix1=x1-wallWidth,iy0=y0+wallWidth,iy1=y1-wallWidth;
    const config=typeof r.stable==='object' && r.stable!==null?r.stable:r;
    const mid=(ix0+ix1)/2,aisle=config.aisle_ft??r.aisle_ft??12,frontL=mid-aisle/2,frontR=mid+aisle/2;
    const stallLength=config.stall_pitch_ft??r.stall_pitch_ft??10, count=Math.min(config.stalls_per_side??r.stalls_per_side??13,Math.floor((iy1-iy0)/stallLength));
    let s=rect(frontL,iy0,aisle,iy1-iy0,`url(#${prefix}-earth)`);
    for(let i=0;i<count;i++) {
      const y=iy0+i*stallLength;
      s+=rect(ix0+.3,y+.3,frontL-ix0-.6,stallLength-.6,`url(#${prefix}-straw)`);
      s+=rect(frontR+.3,y+.3,ix1-frontR-.6,stallLength-.6,`url(#${prefix}-straw)`);
      if(i>0) {
        s+=line(ix0,y,frontL,y,'#574f39',.55)+line(frontR,y,ix1,y,'#574f39',.55);
        s+=line(ix0,y+.25,frontL,y+.25,'#c0a57a',.17,.75)+line(frontR,y+.25,ix1,y+.25,'#c0a57a',.17,.75);
      }
      for(const x of [frontL,frontR]) {
        s+=line(x,y,x,y+3.2,'#594e38',.55)+line(x,y+6.8,x,y+stallLength,'#594e38',.55);
        s+=circle(x,y+.2,.5,'#6d5a3c',1);
      }
    }
    return s;
  }
  function table(r,prefix) {
    if(!r.rect) return '';
    const [x0,y0,x1,y1]=r.rect,cx=(x0+x1)/2,cy=(y0+y1)/2;
    const len=Math.min(24,(x1-x0)*.3),depth=4;
    return rect(cx-len/2+.6,cy-depth/2-.8,len,depth,'#2f352b','opacity=".18"')+
      rect(cx-len/2,cy-depth/2,len,depth,`url(#${prefix}-wood)`,`rx=".35" stroke="#4b4b38" stroke-width=".4"`)+
      line(cx-len/2+.6,cy+depth/2-.5,cx+len/2-.6,cy+depth/2-.5,'#cfb886',.2,.8);
  }
  function groundWeather(r,prefix) {
    const pts=points(r),xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
    const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys),rand=random(r.id||r.name);
    let s='';
    for(let i=0;i<65;i++) {
      const x=x0+rand()*(x1-x0),y=y0+rand()*(y1-y0);
      if(!inside([x,y],pts)) continue;
      const rad=.5+rand()*2;
      s+=circle(x,y,rad,i%3?'#d6c9aa':'#807e69',.06+rand()*.05);
      if(i%5===0) s+=`<path d="M${n(x)} ${n(y)}l.25 1.25 .15 -1.6 .6 1.1" fill="none" stroke="#787a59" stroke-width=".18" opacity=".44"/>`;
    }
    return s;
  }
  function terrain(t,prefix) {
    const pts=terrainPoints(t);
    if(pts.length<2) return '';
    const rand=random('terrain '+(t.id||t.kind)),isLine=t.kind==='path'||t.kind==='contour';
    const meta=Number.isFinite(t.elevation_ft)?` data-elevation-ft="${n(t.elevation_ft)}"`:'';
    let s=`<g data-terrain-kind="${esc(t.kind)}" data-terrain-id="${esc(t.id||t.kind)}"${meta}>`;
    if(isLine) {
      const d=openPath(pts);
      if(t.kind==='contour') s+=`<path d="${d}" fill="none" stroke="#8c654d" stroke-width="${n(t.width_ft??.65)}" opacity=".44" stroke-linecap="round" stroke-linejoin="round"/>`;
      else {
        const w=Math.max(.4,t.width_ft??2);
        s+=`<path d="${d}" fill="none" stroke="#7e5940" stroke-width="${n(w+1)}" opacity=".25" stroke-linecap="round" stroke-linejoin="round"/>`;
        s+=`<path d="${d}" fill="none" stroke="url(#${prefix}-earth)" stroke-width="${n(w)}" stroke-linecap="round" stroke-linejoin="round"/>`;
        s+=`<path d="${d}" fill="none" stroke="#d0a57e" stroke-width="${n(w*.42)}" opacity=".22" stroke-linecap="round" stroke-linejoin="round"/>`;
      }
      return s+'</g>';
    }
    if(pts.length<3) return '';
    const d=path(pts),xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
    const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
    const rocky=t.kind==='rock'||t.kind==='ridge'||(t.kind==='slope'&&prefix.endsWith('-arid'));
    if(t.kind==='ridge') {
      // Cartographic relief along the already supplied ridge footprint. These
      // small shadows indicate the rock lip, not an inferred climbable route.
      s+=`<path d="${d}" fill="#5e3a29" opacity=".1" transform="translate(4,-4)"/>`;
      s+=`<path d="${d}" fill="#633e2b" opacity=".16" transform="translate(2.3,-2.3)"/>`;
      s+=`<path d="${d}" fill="#714832" opacity=".19" transform="translate(.9,-.9)"/>`;
    }
    s+=`<path d="${d}" fill="url(#${prefix}-${rocky?'rock':'earth'})"`+
      (t.kind==='ridge'?' stroke="#82513a" stroke-width="1.2"':t.kind==='slope'?' stroke="#ad7d5b" stroke-width=".35"':'')+'/>';
    if(t.kind==='ridge') s+=`<path d="${d}" fill="none" stroke="#dfb88c" stroke-width=".46" opacity=".62"/>`;
    if(t.kind==='slope') s+=`<path d="${d}" fill="#c89a72" opacity=".1"/>`;
    const clip=prefix+'-terrain-'+hash((t.id||'')+' '+JSON.stringify(t)).toString(36);
    s+=`<defs><clipPath id="${clip}"><path d="${d}"/></clipPath></defs><g clip-path="url(#${clip})">`;
    if(t.kind==='ridge') {
      s+=`<path d="${d}" fill="none" stroke="#67412d" stroke-width="6" opacity=".18"/>`;
      s+=`<path d="${d}" fill="none" stroke="#e3bd92" stroke-width="1" opacity=".5"/>`;
    }
    if((t.kind==='ridge'||t.kind==='slope') && Array.isArray(t.fall) && t.fall.length===2 && t.fall.every(Number.isFinite)) {
      const length=Math.hypot(...t.fall);
      if(length>0) {
        const cx=(x0+x1)/2,cy=(y0+y1)/2,ux=t.fall[0]/length,uy=t.fall[1]/length;
        const projections=pts.map(([x,y])=>x*ux+y*uy),center=cx*ux+cy*uy;
        const lo=Math.min(...projections)-center,hi=Math.max(...projections)-center,relief=clip+'-relief';
        s+=`<defs><linearGradient id="${relief}" gradientUnits="userSpaceOnUse" x1="${n(cx+ux*lo)}" y1="${n(cy+uy*lo)}" x2="${n(cx+ux*hi)}" y2="${n(cy+uy*hi)}"><stop offset="0" stop-color="#e3bc8e" stop-opacity=".25"/><stop offset=".32" stop-color="#bc855f" stop-opacity="0"/><stop offset=".63" stop-color="#70422d" stop-opacity=".16"/><stop offset="1" stop-color="#593a29" stop-opacity="${t.kind==='ridge'?.46:.4}"/></linearGradient></defs>`;
        s+=`<path d="${d}" fill="url(#${relief})"/>`;
      }
    }
    const area=Math.max(1,(x1-x0)*(y1-y0));
    const count=Math.min(t.kind==='scrub'?48:64,Math.max(10,Math.round(area/(t.kind==='scrub'?1800:2500))));
    for(let i=0;i<count;i++) {
      const x=x0+rand()*(x1-x0),y=y0+rand()*(y1-y0);
      if(!inside([x,y],pts)) continue;
      if(t.kind==='scrub') {
        const size=.6+rand()*1.2;
        s+=line(x-size,y,x+size,y+.3,'#827047',.25,.65)+line(x,y-size*.8,x+.25,y+size,'#917447',.23,.65);
        s+=line(x-size*.65,y-size*.65,x+size*.7,y+size*.7,'#6c6744',.18,.58);
      } else if(rocky) {
        const len=2+rand()*7,dy=(rand()-.5)*3;
        s+=`<path d="M${n(x)} ${n(y)}l${n(len*.45)} ${n(dy)} ${n(len*.55)} ${n(-dy*.35)}" fill="none" stroke="#82513a" stroke-width=".35" opacity=".45"/>`;
        s+=line(x,y+.55,x+len*.8,y+dy*.4+.55,'#d5ad85',.22,.33);
      } else {
        const r=.4+rand()*.8;
        s+=circle(x,y,r,'#805b41',.17)+circle(x-.15,y+.25,r*.6,'#d7b48b',.2);
      }
    }
    // Hachures only express a slope direction supplied by the author. No
    // elevations, fall direction or climbability are inferred from a polygon.
    if(Array.isArray(t.fall) && t.fall.length===2 && t.fall.every(Number.isFinite)) {
      const len=Math.hypot(...t.fall);
      if(len>0) for(let i=0;i<Math.min(34,count);i++) {
        const x=x0+rand()*(x1-x0),y=y0+rand()*(y1-y0);
        if(!inside([x,y],pts)) continue;
        const run=10+rand()*9,ux=t.fall[0]/len,uy=t.fall[1]/len;
        for(let k=0;k<3;k++) {
          const offset=(k-1)*1.7,ox=-uy*offset,oy=ux*offset,scale=.65+rand()*.45;
          s+=line(x+ox,y+oy,x+ox+ux*run*scale,y+oy+uy*run*scale,'#825238',.55,.46);
          s+=line(x+ox+.5,y+oy+.35,x+ox+ux*run*scale*.84+.5,y+oy+uy*run*scale*.84+.35,'#ddba91',.26,.38);
        }
      }
    }
    return s+'</g></g>';
  }
  function aridTint(body) {
    const colors={
      '#302f27':'#4b3023','#45493f':'#684431','#d0c6ad':'#d6b18a','#d0c5ad':'#ddbb96',
      '#4b4d43':'#71503a','#c0b49a':'#d2aa83','#514e3c':'#694b30','#686e5e':'#90674e',
      '#c4bba4':'#c9a37e','#747a68':'#9a765a','#a5aa96':'#b4a181','#818a77':'#986d52',
      '#a3a48e':'#b68d69','#717a66':'#977553','#786b50':'#765639','#b0a080':'#b79b6a',
      '#8c8066':'#8f644a','#b8a788':'#c39b75','#8f846a':'#9e7858','#44483c':'#73513a',
      '#c3a778':'#d1ab68','#8c7253':'#805b3e','#a58a63':'#ac8356','#957d58':'#8b6844',
      '#ad9065':'#b18c5c','#9c835c':'#94714a','#a28a64':'#a07b51','#927952':'#866440',
      '#b0a791':'#bb9777','#b4ab94':'#c09e7d','#aba38f':'#b28e6e','#b3aa93':'#bd9876',
      '#aaa48f':'#b39373','#b1aa94':'#bd9e7e','#99927f':'#a88362','#807c69':'#9d7b5b',
      '#414a40':'#684e39','#e2d8b8':'#efd3a2','#c9ba96':'#d4b68b','#63543d':'#735035',
      '#6d6b5e':'#8e6a50','#a6a08b':'#b79978','#858879':'#9e7c60','#bdb39a':'#cab08a',
      '#757c70':'#8c7057','#555e52':'#785c45','#d6c9aa':'#d7b38c','#807e69':'#986f50',
      '#787a59':'#9a8354','#5b6052':'#7c5d43','#3e433a':'#654832','#514f47':'#76513b'
    };
    return body.replace(/#[0-9a-f]{6}/gi,color=>colors[color.toLowerCase()]||color);
  }
  function draw(plan) {
    if(!plan || !Array.isArray(plan.floors)) throw new Error('平面图缺少 floors');
    const all=plan.floors.flatMap(f=>[
      ...f.rooms.flatMap(r=>points(r).map(([x,y])=>[x+(f.dx||0),y])),
      ...(f.terrain||[]).flatMap(t=>terrainPoints(t).flatMap(([x,y])=>{
        const pad=t.kind==='path'?Math.max(.4,t.width_ft??2)/2+.5:t.kind==='contour'?(t.width_ft??.65)/2:0;
        return [[x+(f.dx||0)-pad,y-pad],[x+(f.dx||0)+pad,y+pad]];
      }))
    ]);
    if(!all.length) throw new Error('平面图没有房间');
    const pad=5,x0=Math.min(...all.map(p=>p[0]))-pad,x1=Math.max(...all.map(p=>p[0]))+pad;
    const y0=Math.min(...all.map(p=>p[1]))-pad,y1=Math.max(...all.map(p=>p[1]))+pad;
    const prefix='floor-'+hash(plan.title||'fortress').toString(36)+(plan.palette==='arid-sandstone'?'-arid':'');
    let body=paletteDefs(prefix,plan.palette);
    body+='<g transform="scale(1,-1)">';
    for(const f of plan.floors) {
      body+=`<g transform="translate(${n(f.dx||0)},0)">`;
      for(const t of f.terrain||[]) body+=terrain(t,prefix);
      body+='</g>';
    }
    for(const f of plan.floors) {
      body+=`<g transform="translate(${n(f.dx||0)},0)">`;
      // Floor order is meaningful: outside earth, rooms, then wall-footprints and fixtures.
      for(const r of f.rooms) {
        const material=inferMaterial(r,f),d=path(points(r));
        if(r.condition==='ruin') body+=ruinedBuilding(r,f,prefix);
        else if(isWall(r)) body+=wall(r,prefix);
        else if(/祭坛/.test(r.name)) body+=rubbleAltar(r,prefix);
        else if(r.rect && (r.wall_ft || (['room','hall'].includes(r.kind) && !['tower','cellar'].includes(f.kind) && !/私室|石室/.test(r.name)))) body+=building(r,f,prefix);
        else {
          body+=`<path d="${d}" fill="url(#${prefix}-${material})"`+
            (r.kind==='room'?' stroke="#5b6052" stroke-width=".6"':'')+'/>';
          if(r.kind==='yard') body+=groundWeather(r,prefix);
        }
        if(r.condition!=='ruin' && r.stable!==false && (r.stable || /马厩/.test(r.name))) body+=stable(r,prefix);
        if(r.condition!=='ruin' && /长屋/.test(r.name)) body+=table(r,prefix);
      }
      // Stairs are last so a containing room cannot paint over the flight.
      for(const r of f.rooms) if(r.kind==='stair' || /楼梯|石阶/.test(r.name)) body+=stairs(r,prefix);
      // Door symbols are thresholds, not a statement that an old gate is open.
      for(const d of f.doors||[]) {
        const [[ax,ay],[bx,by]]=d;
        body+=line(ax,ay,bx,by,'#c9ba96',.45,.9);
        body+=circle(ax,ay,.38,'#514e3c',1)+circle(bx,by,.38,'#514e3c',1);
      }
      body+='</g>';
    }
    body+='</g>';
    if(plan.palette==='arid-sandstone') body=aridTint(body);
    const svg=document.createElementNS(NS,'svg');
    svg.setAttribute('xmlns',NS);
    svg.setAttribute('viewBox',`${n(x0)} ${n(-y1)} ${n(x1-x0)} ${n(y1-y0)}`);
    svg.setAttribute('preserveAspectRatio','none');
    svg.setAttribute('aria-hidden','true');
    svg.setAttribute('data-floor-art','fortress');
    if(plan.palette) svg.setAttribute('data-palette',plan.palette);
    svg.innerHTML=body;
    return {svg,bounds:[[y0,x0],[y1,x1]]};
  }
  window.AtlasFloorArt={draw};
})();
