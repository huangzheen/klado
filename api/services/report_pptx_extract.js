// Runs inside the rendered report. No network calls and no model calls.
// Geometry is measured after the Report Deck runtime and bundled fonts load.
(lang) => {
  try {
    const deck = document.querySelector('.deck');
    if (!deck) throw new Error('Only 16:9 Report Decks can be exported to PPTX.');
    const slides = [...deck.querySelectorAll('.slide')].filter(s => {
      const code = (s.getAttribute('data-lang') || '').toLowerCase();
      return !code || code === (lang || document.documentElement.getAttribute('data-deck-lang') || 'en').toLowerCase();
    });
    if (!slides.length) throw new Error('No slides found for the selected language.');
    // An authored report may publish one data model for both its HTML render
    // and PPTX export.  Interactive reports replace window.reportDeckModel with
    // their hydrated state after GET /state; the exporter reads it only after
    // network idle.  Older reports without a model remain fully supported.
    const modelTag=document.querySelector('script[type="application/json"][data-report-deck-model]');
    let model=window.reportDeckModel;
    if (model==null && modelTag) {
      if (modelTag.textContent.length>262144) throw new Error('Report Deck model is too large.');
      try { model=JSON.parse(modelTag.textContent); }
      catch (_) { throw new Error('Invalid Report Deck model JSON.'); }
    }
    if (model!=null && (typeof model!=='object' || Array.isArray(model) || model.version!==1))
      throw new Error('Report Deck model must be an object with version: 1.');
    const objectIds=new Set();
    function modelValue(path) {
      if (!model) throw new Error('A data-pptx binding needs a Report Deck model.');
      if (!/^[A-Za-z_$][\w$]*(?:\.(?:[A-Za-z_$][\w$]*|\d+))*$/.test(path))
        throw new Error('Invalid Report Deck model path: '+path);
      let value=model;
      for (const part of path.split('.')) {
        if (value==null || !Object.prototype.hasOwnProperty.call(value,part))
          throw new Error('Missing Report Deck model value: '+path);
        value=value[part];
      }
      return value;
    }
    function normText(value) { return String(value).replace(/\s+/g,' ').trim(); }
    deck.style.position = 'relative';
    deck.style.inset = 'auto';
    deck.style.width = '1920px';
    deck.style.height = 'auto';
    deck.style.overflow = 'visible';
    for (const slide of slides) {
      slide.style.display = 'flex';
      slide.style.position = 'relative';
      slide.style.left = '0';
      slide.style.top = '0';
      slide.style.transform = 'none';
      slide.style.margin = '0';
    }
    const result = [];
    function box(r, root) {
      const origin = root instanceof Element ? root.getBoundingClientRect() : root;
      return {x:r.left-origin.left, y:r.top-origin.top, w:r.width, h:r.height};
    }
    function rgba(value) {
      if (!value || value==='transparent') return [0,0,0,0];
      const match=value.match(/^rgba?\(([^)]+)\)$/i);
      if (!match) throw new Error('Unsupported CSS colour: '+value);
      const parts=match[1].replace(/\s*\/\s*/,',').split(/[,\s]+/).filter(Boolean).map(Number);
      return [parts[0],parts[1],parts[2],parts.length>3?parts[3]:1];
    }
    function opaqueBackground(el) {
      // PowerPoint table-cell fills have no CSS alpha. Composite every ancestor
      // layer exactly as Chromium paints it, including the coloured panel under
      // translucent heat-map cells and row labels.
      const layers=[];
      for (let node=el;node instanceof Element;node=node.parentElement)
        layers.unshift(rgba(getComputedStyle(node).backgroundColor));
      let rgb=[255,255,255];
      for (const layer of layers) rgb=rgb.map((v,i)=>layer[i]*layer[3]+v*(1-layer[3]));
      return `rgb(${rgb.map(Math.round).join(', ')})`;
    }
    function paintedColor(value, backdrop) {
      const fg=rgba(value), bg=rgba(backdrop || 'rgb(255, 255, 255)');
      return `rgb(${fg.slice(0,3).map((v,i)=>Math.round(v*fg[3]+bg[i]*(1-fg[3]))).join(', ')})`;
    }
    function shadows(value) {
      if (!value || value==='none') return [];
      const parts=value.split(/,(?![^()]*\))/);
      return parts.map(part=>{
        const color=(part.match(/rgba?\([^)]+\)|#[\da-f]{3,8}/i)||[])[0];
        const offsets=part.replace(color||'','').match(/-?\d+(?:\.\d+)?px/g)||[];
        if (!color || offsets.length<2) throw new Error('Unsupported CSS box shadow.');
        return {inset:/\binset\b/.test(part),color,opacity:rgba(color)[3],
          x:parseFloat(offsets[0]),y:parseFloat(offsets[1]),
          blur:parseFloat(offsets[2]||0),spread:parseFloat(offsets[3]||0)};
      });
    }
    function insetLines(b, shadow, nodes) {
      // Axis-aligned, opaque inset edges can mark meaningful annotations.
      // Spread rings and translucent shadows are surface polish, and exporting
      // them as connectors creates cell frames absent from the browser view.
      if (shadow.opacity < .999 || shadow.spread) return;
      if (!shadow.inset || shadow.blur || (shadow.x && shadow.y))
        throw new Error('Unsupported inset shadow.');
      const width=Math.abs(shadow.x||shadow.y)||shadow.spread;
      if (!width) return;
      const inset=shadow.spread ? shadow.spread/2 : width/2;
      if (shadow.spread || shadow.y>0)
        nodes.push({kind:'line',x:b.x,y:b.y+inset,w:b.w,h:0,color:shadow.color,width});
      if (shadow.spread || shadow.y<0)
        nodes.push({kind:'line',x:b.x,y:b.y+b.h-inset,w:b.w,h:0,color:shadow.color,width});
      if (shadow.spread || shadow.x>0)
        nodes.push({kind:'line',x:b.x+inset,y:b.y,w:0,h:b.h,color:shadow.color,width});
      if (shadow.spread || shadow.x<0)
        nodes.push({kind:'line',x:b.x+b.w-inset,y:b.y,w:0,h:b.h,color:shadow.color,width});
    }
    function style(s, el) {
      return {
        font:s.fontFamily, size:parseFloat(s.fontSize)||18,
        bold:parseInt(s.fontWeight,10)>=600, italic:s.fontStyle==='italic',
        underline:s.textDecorationLine.includes('underline'),
        color:paintedColor(s.color,el?opaqueBackground(el):null), align:s.textAlign,
        background:el?opaqueBackground(el):s.backgroundColor,
        lineHeight:parseFloat(s.lineHeight)||0
      };
    }
    function borders(s, el) {
      const result = {};
      const backdrop=el?opaqueBackground(el):null;
      for (const side of ['top','right','bottom','left']) {
        const raw=s['border'+side[0].toUpperCase()+side.slice(1)+'Color'];
        const width=parseFloat(s['border'+side[0].toUpperCase()+side.slice(1)+'Width'])||0;
        result[side] = {color:paintedColor(raw,backdrop),width:rgba(raw)[3]>0?width:0};
      }
      return result;
    }
    function meter(el, nodes, root) {
      const r = el.getBoundingClientRect();
      const b = box(r,root);
      const soft = el.classList.contains('soft');
      nodes.push({kind:'shape',...b,fill:soft?'#e3e9f1':'#dfe5ee',radius:3});
      const pct = Math.max(0,Math.min(100,parseFloat(getComputedStyle(el).getPropertyValue('--v'))||0));
      if (pct) nodes.push({kind:'shape',...b,w:b.w*pct/100,fill:soft?'#9db1c9':'#2563eb',radius:3});
    }
    function flattenable(node) {
      if (node.nodeType===Node.TEXT_NODE) return true;
      if (node.nodeType!==Node.ELEMENT_NODE) return false;
      const tag=node.tagName.toLowerCase();
      if (tag==='br') return true;
      if (node.hasAttribute('data-pptx-role') || node.hasAttribute('data-pptx-export') ||
          node.hasAttribute('data-pptx-bind') || node.hasAttribute('data-pptx-chart-ref')) return false;
      if (['img','canvas','svg','video','iframe','object','button','input','textarea','select'].includes(tag) ||
          node.hasAttribute('data-pptx-chart') || node.classList.contains('s-meter')) return false;
      const s=getComputedStyle(node);
      if (s.display==='none'||s.visibility==='hidden'||parseFloat(s.opacity)===0) return true;
      if (!['inline','contents'].includes(s.display) || s.backgroundImage!=='none' ||
          s.boxShadow!=='none' || s.filter!=='none' || s.clipPath!=='none' ||
          !isPureTranslation(s.transform)) return false;
      for (const pseudo of ['::before','::after']) {
        const content=getComputedStyle(node,pseudo).content;
        if (content!=='none' && content!=='normal' && content!=='""') return false;
      }
      if (s.backgroundColor!=='rgba(0, 0, 0, 0)' && s.backgroundColor!=='transparent') return false;
      if (['top','right','bottom','left'].some(side =>
          parseFloat(s['border'+side[0].toUpperCase()+side.slice(1)+'Width'])>0)) return false;
      return [...node.childNodes].every(flattenable);
    }
    function textRuns(nodes) {
      const raw=[];
      function visit(node) {
        if (node.nodeType===Node.TEXT_NODE) {
          raw.push({text:node.textContent,style:style(getComputedStyle(node.parentElement),node.parentElement)});
        } else if (node.nodeType===Node.ELEMENT_NODE) {
          if (node.tagName.toLowerCase()==='br') raw.push({text:'\v',style:style(getComputedStyle(node.parentElement),node.parentElement)});
          else {
            const s=getComputedStyle(node);
            if (s.display!=='none' && s.visibility!=='hidden' && parseFloat(s.opacity)!==0) {
              if (raw.length && parseFloat(s.marginLeft)>0 && !/\s$/.test(raw[raw.length-1].text))
                raw.push({text:' ',style:style(getComputedStyle(node.parentElement),node.parentElement)});
              [...node.childNodes].forEach(visit);
            }
          }
        }
      }
      nodes.forEach(visit);
      const runs=[];
      let content='';
      for (const item of raw) {
        if (item.text==='\v') { runs.push(item); content+='\v'; continue; }
        let value=item.text.replace(/\s+/g,' ');
        if (!content || content.endsWith(' ') || content.endsWith('\v')) value=value.replace(/^ /,'');
        if (!value) continue;
        runs.push({text:value,style:item.style});
        content+=value;
      }
      while (runs.length && !runs[runs.length-1].text.trim()) runs.pop();
      if (runs.length) runs[runs.length-1].text=runs[runs.length-1].text.replace(/ +$/,'');
      return runs.filter(run=>run.text);
    }
    function isPureTranslation(value) {
      if (value==='none') return true;
      try {
        const m=new DOMMatrixReadOnly(value);
        const near=(a,b)=>Math.abs(a-b)<1e-6;
        // getBoundingClientRect and DOM Ranges already include a CSS translate.
        // Rotation, skew and scale change the painted geometry and need their
        // own native PowerPoint representation, so keep rejecting them.
        return m.is2D && near(m.a,1) && near(m.b,0) && near(m.c,0) && near(m.d,1);
      } catch (_) { return false; }
    }
    function addTextSegment(el, children, nodes, root) {
      const runs=textRuns(children);
      if (!runs.length) return;
      const range=document.createRange();
      range.setStartBefore(children[0]);
      range.setEndAfter(children[children.length-1]);
      const r=range.getBoundingClientRect(), er=el.getBoundingClientRect(), s=getComputedStyle(el);
      if (r.width<=0||r.height<=0) return;
      const right=er.right-(parseFloat(s.paddingRight)||0)-(parseFloat(s.borderRightWidth)||0);
      const oneLine=r.height<=(parseFloat(s.lineHeight)||parseFloat(s.fontSize)||18)*1.3;
      const centeredFlex=s.display==='flex' && s.alignItems==='center' && s.justifyContent==='center';
      if (centeredFlex) {
        const left=er.left+(parseFloat(s.borderLeftWidth)||0)+(parseFloat(s.paddingLeft)||0);
        const top=er.top+(parseFloat(s.borderTopWidth)||0)+(parseFloat(s.paddingTop)||0);
        nodes.push({kind:'text',...box({left,top,width:right-left,
          height:er.bottom-(parseFloat(s.borderBottomWidth)||0)-(parseFloat(s.paddingBottom)||0)-top},root),
          paragraphs:[{runs,style:{...style(s,el),align:'center'}}],wrap:true,valign:'middle'});
        return;
      }
      const flexItem=el.parentElement && ['flex','inline-flex'].includes(getComputedStyle(el.parentElement).display);
      // PowerPoint's font metrics can be wider than Chromium's (especially when
      // a web font is unavailable locally). Give one-line runs breathing room so
      // short labels such as "53%" do not break inside their own badge.
      const width=flexItem ? r.width : Math.max(r.width*(oneLine?1.35:1),right-r.left);
      nodes.push({kind:'text',...box({left:r.left,top:r.top,width,height:Math.max(r.height,parseFloat(s.lineHeight)||0)},root),
                  paragraphs:[{runs,style:style(s,el)}],
                  wrap:!oneLine,fit:flexItem});
    }
    function pseudoRect(el, pseudo, nodes, root) {
      const p=getComputedStyle(el,pseudo);
      if (p.content!=='""' || p.display==='none' || p.position!=='absolute' ||
          p.backgroundImage!=='none' || rgba(p.backgroundColor)[3]===0) return;
      const s=getComputedStyle(el), er=el.getBoundingClientRect();
      const leftEdge=er.left+(parseFloat(s.borderLeftWidth)||0);
      const topEdge=er.top+(parseFloat(s.borderTopWidth)||0);
      const innerW=er.width-(parseFloat(s.borderLeftWidth)||0)-(parseFloat(s.borderRightWidth)||0);
      const innerH=er.height-(parseFloat(s.borderTopWidth)||0)-(parseFloat(s.borderBottomWidth)||0);
      const length=(value,full)=>value.endsWith('%')?parseFloat(value)*full/100:parseFloat(value);
      const left=p.left!=='auto'?length(p.left,innerW):innerW-length(p.right,innerW)-parseFloat(p.width);
      const top=p.top!=='auto'?length(p.top,innerH):innerH-length(p.bottom,innerH)-parseFloat(p.height);
      const w=p.width!=='auto'?length(p.width,innerW):innerW-left-length(p.right,innerW);
      const h=p.height!=='auto'?length(p.height,innerH):innerH-top-length(p.bottom,innerH);
      if (![left,top,w,h].every(Number.isFinite) || w<=0 || h<=0) return;
      nodes.push({kind:'shape',...box({left:leftEdge+left,top:topEdge+top,width:w,height:h},root),
        fill:paintedColor(p.backgroundColor,opaqueBackground(el))});
    }
    function addListText(el, nodes, root) {
      const items=[...el.children];
      function simpleItem(li) {
        if (li.tagName.toLowerCase()!=='li' || ![...li.childNodes].every(flattenable)) return false;
        const s=getComputedStyle(li);
        if (s.backgroundImage!=='none' || s.boxShadow!=='none' || s.filter!=='none' ||
            s.clipPath!=='none' || s.transform!=='none') return false;
        if (s.backgroundColor!=='rgba(0, 0, 0, 0)' && s.backgroundColor!=='transparent') return false;
        if (['top','right','bottom','left'].some(side =>
            parseFloat(s['border'+side[0].toUpperCase()+side.slice(1)+'Width'])>0)) return false;
        return ['::before','::after'].every(pseudo=>{
          const content=getComputedStyle(li,pseudo).content;
          return content==='none'||content==='normal'||content==='""';
        });
      }
      if (!items.length || items.some(li=>!simpleItem(li))) return false;
      const rows=items.map(li=>{
        const range=document.createRange();
        range.selectNodeContents(li);
        return {li,rect:range.getBoundingClientRect(),runs:textRuns([...li.childNodes]),
                style:style(getComputedStyle(li),li)};
      });
      if (rows.some(row=>!row.runs.length || row.rect.width<=0)) return false;
      const left=Math.min(...rows.map(row=>row.rect.left));
      const top=rows[0].rect.top;
      const right=Math.max(...rows.map(row=>row.li.getBoundingClientRect().right));
      const bottom=Math.max(...rows.map(row=>row.li.getBoundingClientRect().bottom));
      const paragraphs=rows.map((row,i)=>({runs:row.runs,style:row.style,
        spaceAfter:i+1<rows.length ? Math.max(0,rows[i+1].li.getBoundingClientRect().top-row.li.getBoundingClientRect().bottom) : 0}));
      nodes.push({kind:'text',...box({left,top,width:right-left,height:bottom-top},root),paragraphs,wrap:true});
      return true;
    }
    function collect(el, nodes, root) {
      if (!(el instanceof Element)) return;
      const s = getComputedStyle(el);
      if (s.display==='none'||s.visibility==='hidden'||parseFloat(s.opacity)===0) return;
      const r = el.getBoundingClientRect();
      if (r.width<=0||r.height<=0) return;
      const objectId=el.getAttribute('data-pptx-id');
      if (objectId) {
        if (objectIds.has(objectId)) throw new Error('Duplicate data-pptx-id: '+objectId);
        objectIds.add(objectId);
      }
      const exportAs=el.getAttribute('data-pptx-export')||'native';
      if (!['native','omit','image'].includes(exportAs))
        throw new Error('Invalid data-pptx-export: '+exportAs);
      if (exportAs==='omit') return;
      if (exportAs==='image') {
        const id='pptx-raster-'+document.querySelectorAll('[data-pptx-raster-id]').length;
        el.setAttribute('data-pptx-raster-id',id);
        nodes.push({kind:'raster',...box(r,root),id,includeChildren:true});
        return;
      }
      const binding=el.getAttribute('data-pptx-bind');
      if (binding) {
        const expected=modelValue(binding);
        if (typeof expected==='object' || normText(el.innerText)!==normText(expected))
          throw new Error('Report Deck model and rendered text differ at '+binding);
      }
      const tag = el.tagName.toLowerCase();
      if (['canvas','svg','video','iframe','object','input','textarea','select'].includes(tag))
        throw new Error('Unsupported '+tag+' element. Supply an image or a data-pptx-chart specification.');
      const chartRef=el.getAttribute('data-pptx-chart-ref');
      if (chartRef || el.hasAttribute('data-pptx-chart')) {
        let data=chartRef ? modelValue(chartRef) : null;
        if (!chartRef) {
          try { data=JSON.parse(el.getAttribute('data-pptx-chart')); }
          catch (_) { throw new Error('Invalid data-pptx-chart JSON.'); }
        }
        if (!data || typeof data!=='object' || Array.isArray(data))
          throw new Error('Chart model must be an object.');
        nodes.push({kind:'chart',...box(r,root),data});
        return;
      }
      if (el.getAttribute('data-pptx-role')==='chart')
        throw new Error('A native chart needs data-pptx-chart-ref or data-pptx-chart.');
      if (el.classList.contains('s-meter')) { meter(el,nodes,root); return; }
      if (tag==='img') {
        if (!el.complete || !el.naturalWidth) throw new Error('An image did not load.');
        const canvas=document.createElement('canvas');
        canvas.width=el.naturalWidth; canvas.height=el.naturalHeight;
        try {
          canvas.getContext('2d').drawImage(el,0,0);
          nodes.push({kind:'image',...box(r,root),data:canvas.toDataURL('image/png')});
        } catch (_) { throw new Error('An image cannot be embedded; use an in-app or data URL.'); }
        return;
      }
      if (tag==='table') {
        const visibleRows=[...el.rows].filter(row=>getComputedStyle(row).display!=='none');
        const tableRef=el.getAttribute('data-pptx-table-ref');
        if (tableRef) {
          const value=modelValue(tableRef);
          if (!Array.isArray(value) || value.length!==visibleRows.length ||
              visibleRows.some((row,i)=>!Array.isArray(value[i]) || value[i].length!==row.cells.length ||
                [...row.cells].some((cell,j)=>normText(cell.innerText)!==normText(value[i][j]))))
            throw new Error('Report Deck model and rendered table differ at '+tableRef);
        }
        const rows=visibleRows.map(row=>
          [...row.cells].map(cell=>{
            const c=getComputedStyle(cell), b=box(cell.getBoundingClientRect(),root);
            const children=[...cell.children].filter(child=>getComputedStyle(child).display!=='none');
            // Native table-cell text is vertically anchored by PowerPoint.
            // When a cell also contains badges or other positioned children,
            // place its direct text at the measured DOM range instead.
            let directText=children.length ? '' : cell.innerText.trim();
            if (!directText && !children.length) {
              const pseudo=getComputedStyle(cell,'::before').content;
              if (/^"[^"\\]*"$/.test(pseudo)) directText=pseudo.slice(1,-1);
            }
            return {...b,text:directText,style:style(c,cell),borders:borders(c,cell),
              padding:{left:parseFloat(c.paddingLeft)||0,right:parseFloat(c.paddingRight)||0,
                       top:parseFloat(c.paddingTop)||0,bottom:parseFloat(c.paddingBottom)||0},
              verticalAlign:c.verticalAlign,
              rowspan:cell.rowSpan,colspan:cell.colSpan,shadows:shadows(c.boxShadow)};
          }));
        nodes.push({kind:'table',...box(r,root),rows,
                    rowHeights:visibleRows.map(row=>row.getBoundingClientRect().height)});
        for (let i=0;i<visibleRows.length;i++) for (let j=0;j<visibleRows[i].cells.length;j++) {
          const element=visibleRows[i].cells[j], cell=rows[i][j];
          if (element.children.length) {
            let segment=[];
            function flushCellText() {
              if (segment.some(node=>node.textContent.trim())) addTextSegment(element,segment,nodes,root);
              segment=[];
            }
            for (const child of element.childNodes) {
              if (child.nodeType===Node.TEXT_NODE) segment.push(child);
              else flushCellText();
            }
            flushCellText();
          }
          for (const child of element.children) collect(child,nodes,root);
          for (const shadow of [...cell.shadows].reverse()) insetLines(cell,shadow,nodes);
        }
        for (const m of el.querySelectorAll('.s-meter')) meter(m,nodes,root);
        return;
      }
      if (s.filter!=='none'||s.clipPath!=='none'||!isPureTranslation(s.transform))
        throw new Error('CSS filter, clipping, rotation, scaling or skew cannot be kept editable in PPTX.');
      const shadow=shadows(s.boxShadow);
      // Keep crisp, opaque CSS rings as editable outlines, but omit blurred
      // drop shadows and translucent rings that look much darker in PPT.
      const outerShadow=shadow.find(item=>!item.inset && !item.blur &&
        item.spread>=1 && item.opacity>=.999)||null;
      for (const pseudo of ['::before','::after']) {
        const content=getComputedStyle(el,pseudo).content;
        if (content!=='none' && content!=='normal' && content!=='""' &&
            !(el.isContentEditable && !el.textContent.trim()))
          throw new Error('CSS pseudo-element content needs an explicit HTML element for PPTX export.');
      }
      const raster=s.backgroundImage!=='none';
      const label=(el.matches('.badge, .mini-badge, .rrp, .channel-badge') ||
        el.getAttribute('data-pptx-role')==='badge') && !raster &&
        [...el.childNodes].every(flattenable);
      if (el!==root && label) {
        const runs=textRuns([...el.childNodes]);
        if (runs.length) {
          const bd=borders(s,el), edges=Object.values(bd);
          const uniform=edges.every(edge=>edge.width===edges[0].width && edge.color===edges[0].color);
          if (uniform) {
            const cssEdge=edges[0].width>0 ? edges[0] : null;
            const ring=!cssEdge && outerShadow ? outerShadow : null;
            const width=cssEdge ? cssEdge.width : ring ? ring.spread : 0;
            const inset=cssEdge ? width/2 : 0;
            const outset=ring ? width/2 : 0;
            const b=box(r,root), offset=inset-outset;
            const labelStyle={...style(s,el),align:'center'};
            nodes.push({kind:'badge',x:b.x+offset,y:b.y+offset,
              w:b.w-2*offset,h:b.h-2*offset,fill:opaqueBackground(el),
              stroke:width?{color:cssEdge?cssEdge.color:ring.color,width}:null,
              radius:Math.max(0,(parseFloat(s.borderTopLeftRadius)||0)-offset),
              // The measured badge box already includes CSS padding.  Keeping
              // that padding as PowerPoint text insets leaves only the exact
              // Chromium glyph width.  A slide paste can substitute the font,
              // then Office's auto-fit shrinks the text down to dots.  Center
              // fixed-size text in the full native badge instead.
              paragraphs:[{runs,style:labelStyle}],wrap:false,fit:false,valign:'middle',
              margins:{left:0,right:0,top:0,bottom:0}});
            return;
          }
        }
      }
      if (el!==root) {
        const b=box(r,root), bd=borders(s,el);
        const hasBorder=Object.values(bd).some(v=>v.width>0);
        const hasFill=rgba(s.backgroundColor)[3]>0;
        if (raster) {
          const id='pptx-raster-'+document.querySelectorAll('[data-pptx-raster-id]').length;
          el.setAttribute('data-pptx-raster-id',id);
          nodes.push({kind:'raster',...b,id});
        } else if (hasBorder||hasFill||outerShadow)
          nodes.push({kind:'shape',...b,fill:hasFill?opaqueBackground(el):null,
                      borders:bd,radius:parseFloat(s.borderTopLeftRadius)||0,
                      shadow:outerShadow});
        if (!raster) for (const item of [...shadow].reverse()) if (item.inset) insetLines(b,item,nodes);
        if (!raster) for (const pseudo of ['::before','::after']) pseudoRect(el,pseudo,nodes,root);
      }
      if ((tag==='ul'||tag==='ol') && addListText(el,nodes,root)) return;
      let segment=[];
      function flush() { if (segment.length) addTextSegment(el,segment,nodes,root); segment=[]; }
      for (const child of el.childNodes) {
        if (flattenable(child)) segment.push(child);
        else if (child.nodeType===Node.ELEMENT_NODE) { flush(); collect(child,nodes,root); }
      }
      flush();
    }
    for (const slide of slides) {
      const nodes=[];
      for (const child of slide.children) collect(child,nodes,slide);
      if (nodes.length>8000) throw new Error('A slide has too many editable elements.');
      result.push({background:opaqueBackground(slide),nodes});
    }
    return {slides:result};
  } catch (error) {
    return {error:error.message||String(error)};
  }
}
