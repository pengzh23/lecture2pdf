-- 把 Markdown 中的 ::: {.definition title="..."} 等块转换为 LaTeX 彩色框（见 preamble.tex），
-- 其他输出格式（HTML / typst）则在块首加粗显示「定义（标题）」。

-- kind: box = 带编号彩色框；named = 不编号、标题原样显示的框；inline = 无框，标签与首段同行
local KINDS = {
  definition  = { env = "definition",  label = "定义", kind = "box" },
  theorem     = { env = "theorem",     label = "定理", kind = "box" },
  proposition = { env = "proposition", label = "命题", kind = "box" },
  property    = { env = "property",    label = "性质", kind = "box" },
  lemma       = { env = "lemma",       label = "引理", kind = "box" },
  corollary   = { env = "corollary",   label = "推论", kind = "box" },
  postulate   = { env = "postulate",   label = "公设", kind = "named" },
  axiom       = { env = "postulate",   label = "公理", kind = "named" },
  example     = { env = "example",     label = "例",   kind = "inline", color = "l2pblue" },
  remark      = { env = "remark",      label = "注",   kind = "inline", color = "l2pred" },
  proof       = { env = "proofbox",    label = "证明", kind = "inline", color = "l2pred" },
}

local function title_inlines(s)
  if s == nil or s == "" then return nil end
  local doc = pandoc.read(s, "markdown")
  if #doc.blocks == 0 or doc.blocks[1].content == nil then return nil end
  return doc.blocks[1].content
end

local function to_latex(inlines)
  local out = pandoc.write(pandoc.Pandoc({ pandoc.Plain(inlines) }), "latex")
  return (out:gsub("\n", " "))
end

-- 把标签插到第一段开头；第一块不是段落（如列表）时单独成段
local function prepend_label(content, label_inlines)
  local first = content[1]
  if first and (first.t == "Para" or first.t == "Plain") then
    for i = #label_inlines, 1, -1 do table.insert(first.content, 1, label_inlines[i]) end
  else
    table.insert(content, 1, pandoc.Para(label_inlines))
  end
end

local function latex_div(spec, title, content)
  local open
  if spec.kind == "named" then
    open = "\\begin{" .. spec.env .. "}{" .. (title and to_latex(title) or spec.label) .. "}"
  elseif spec.env == "proofbox" then
    open = "\\begin{proofbox}"
  else
    open = "\\begin{" .. spec.env .. "}{" .. (title and to_latex(title) or "") .. "}{}"
  end
  if spec.kind == "inline" then
    local lab = spec.env == "proofbox" and "证明." or "\\tcbtitle"
    prepend_label(content, {
      pandoc.RawInline("latex", "{\\color{" .. spec.color .. "}\\bfseries " .. lab .. "}\\quad{}")
    })
  end
  local blocks = { pandoc.RawBlock("latex", open) }
  for _, b in ipairs(content) do table.insert(blocks, b) end
  table.insert(blocks, pandoc.RawBlock("latex", "\\end{" .. spec.env .. "}"))
  return blocks
end

function Div(el)
  local spec
  for _, c in ipairs(el.classes) do
    if KINDS[c] then spec = KINDS[c]; break end
  end
  if not spec then return nil end
  local title = title_inlines(el.attributes.title or el.attributes.name)

  if FORMAT:match("latex") then
    return latex_div(spec, title, el.content)
  end

  local label = { pandoc.Str(spec.label) }
  if title then
    if spec.kind == "named" then
      label = title
    else
      table.insert(label, pandoc.Str("（"))
      for _, i in ipairs(title) do table.insert(label, i) end
      table.insert(label, pandoc.Str("）"))
    end
  end
  prepend_label(el.content, { pandoc.Strong(label), pandoc.Space() })
  el.attributes.title = nil
  el.attributes.name = nil
  el.classes:insert("l2p-box")
  return el
end
