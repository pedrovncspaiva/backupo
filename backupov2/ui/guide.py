"""The Help window and the About box.

Both are reference material rather than controls, so they are kept out of
``app.py``: the main window stays about the batch in front of you, and
everything that explains the batch lives here.

The guides are written as plain data at the bottom of this file - a topic is a
title plus a list of blocks - so adding one is editing a list, not a layout.
"""

from __future__ import annotations

import platform
import sys
import tkinter as tk
from tkinter import ttk

from .. import __version__
from ..strings import APP_TITLE
from . import theme
from .widgets import Card


class _Window(tk.Toplevel):
    """Shared plumbing: branded, centred on the parent, Esc closes."""

    def __init__(self, parent, title: str, width: int, height: int) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.configure(background=theme.CANVAS)
        theme.apply_window_icon(self)

        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)


class HelpWindow(_Window):
    """Short, task-shaped guides: pick a question on the left, read on the right.

    A single scrolling wall of text is what a README is for. This is the
    version you open mid-batch with a disc in your hand, so each topic answers
    one question and fits on one screen.
    """

    def __init__(self, parent, topic: str | None = None) -> None:
        super().__init__(parent, "Ajuda - " + APP_TITLE, 900, 620)
        self.minsize(720, 480)

        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # -- topic list ---------------------------------------------------
        sidebar = tk.Frame(self, background=theme.SURFACE, padx=0, pady=0,
                           highlightthickness=1, highlightbackground=theme.BORDER)
        sidebar.grid(row=0, column=0, sticky="ns", padx=(12, 0), pady=12)
        sidebar.rowconfigure(1, weight=1)

        tk.Label(
            sidebar,
            text="COMO FAZER",
            font=theme.FONT_SMALL_BOLD,
            background=theme.SURFACE,
            foreground=theme.INK_MUTED,
            anchor="w",
            padx=14,
            pady=10,
        ).grid(row=0, column=0, sticky="ew")

        self.topics = ttk.Treeview(sidebar, show="tree", selectmode="browse", height=18)
        self.topics.column("#0", width=232, stretch=False)
        self.topics.grid(row=1, column=0, sticky="nsew")
        self.topics.tag_configure("topic", font=theme.FONT_BODY)
        for key, (title, _blocks) in TOPICS.items():
            self.topics.insert("", "end", iid=key, text="   " + title, tags=("topic",))
        self.topics.bind("<<TreeviewSelect>>", self._show_selected)

        # -- article ------------------------------------------------------
        article = Card(self, padding=0)
        article.grid(row=0, column=1, sticky="nsew", padx=12, pady=12)
        article.columnconfigure(0, weight=1)
        article.rowconfigure(0, weight=1)

        self.body = tk.Text(
            article,
            wrap="word",
            font=theme.FONT_BODY,
            background=theme.SURFACE,
            foreground=theme.INK,
            relief="flat",
            highlightthickness=0,
            padx=22,
            pady=18,
            cursor="arrow",
            spacing1=2,
            spacing3=4,
        )
        self.body.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(article, orient="vertical", command=self.body.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.body.configure(yscrollcommand=scroll.set)

        self.body.tag_configure("h1", font=(theme.UI_FAMILY, 16, "bold"),
                                foreground=theme.BRAND_DEEP, spacing3=10)
        self.body.tag_configure("h2", font=(theme.UI_FAMILY, 11, "bold"),
                                foreground=theme.INK, spacing1=14, spacing3=6)
        self.body.tag_configure("p", font=theme.FONT_BODY, foreground=theme.INK_SOFT,
                                spacing3=8, lmargin1=0, lmargin2=0)
        self.body.tag_configure("step", font=theme.FONT_BODY, foreground=theme.INK,
                                lmargin1=6, lmargin2=30, spacing3=6)
        self.body.tag_configure("bullet", font=theme.FONT_BODY, foreground=theme.INK,
                                lmargin1=6, lmargin2=22, spacing3=6)
        self.body.tag_configure("key", font=(theme.MONO_FAMILY, 9, "bold"),
                                background=theme.CANVAS, foreground=theme.BRAND_DEEP)
        self.body.tag_configure("note", font=theme.FONT_SMALL, foreground=theme.WARNING,
                                background=theme.WARNING_TINT, lmargin1=10, lmargin2=10,
                                rmargin=10, spacing1=8, spacing3=8)
        self.body.tag_configure("strong", font=theme.FONT_BODY_BOLD, foreground=theme.INK)

        footer = ttk.Frame(self, style="TFrame")
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        ttk.Label(
            footer,
            text="O README do projeto traz a referencia completa, com tabelas e imagens.",
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Button(footer, text="Fechar", command=self.destroy, style="Accent.TButton").pack(
            side="right"
        )

        first = topic if topic in TOPICS else next(iter(TOPICS))
        self.topics.selection_set(first)
        self.topics.focus(first)

    def _show_selected(self, _event=None) -> None:
        selection = self.topics.selection()
        if selection:
            self.render(selection[0])

    def render(self, key: str) -> None:
        title, blocks = TOPICS[key]
        self.body.configure(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("end", title + "\n", "h1")
        for kind, text in blocks:
            if kind == "step":
                self.body.insert("end", text + "\n", "step")
            elif kind == "bullet":
                self.body.insert("end", "•  " + text + "\n", "bullet")
            elif kind == "key":
                shortcut, meaning = text
                self.body.insert("end", f" {shortcut} ", "key")
                self.body.insert("end", f"   {meaning}\n", "bullet")
            else:
                self.body.insert("end", text + "\n", kind)
        self.body.configure(state="disabled")
        self.body.yview_moveto(0)


class AboutDialog(_Window):
    """Who made this, which version it is, and what it is built on."""

    def __init__(self, parent) -> None:
        super().__init__(parent, "Sobre o " + APP_TITLE, 560, 680)
        self.resizable(False, False)

        outer = tk.Frame(self, background=theme.CANVAS, padx=16, pady=16)
        outer.pack(fill="both", expand=True)

        # -- identity -----------------------------------------------------
        banner = tk.Frame(outer, background=theme.SURFACE, padx=20, pady=14,
                          highlightthickness=1, highlightbackground=theme.BORDER)
        banner.pack(fill="x")

        logo = theme.load_logo(self, 48)
        if logo is not None:
            tk.Label(banner, image=logo, background=theme.SURFACE).pack()

        tk.Label(banner, text=APP_TITLE, font=(theme.UI_FAMILY, 15, "bold"),
                 background=theme.SURFACE, foreground=theme.BRAND_DEEP).pack(pady=(8, 0))
        tk.Label(banner, text=f"Versao {__version__}", font=theme.FONT_BODY,
                 background=theme.SURFACE, foreground=theme.INK_SOFT).pack(pady=(2, 0))
        tk.Label(
            banner,
            text=("Copia lotes de CDs e DVDs para pastas organizadas,\n"
                  "um disco por pasta, sem supervisao."),
            font=theme.FONT_SMALL,
            justify="center",
            background=theme.SURFACE,
            foreground=theme.INK_MUTED,
        ).pack(pady=(8, 0))

        wordmark = tk.Frame(banner, background=theme.SURFACE)
        wordmark.pack(pady=(12, 0))
        tk.Label(wordmark, text="SONDOTECNICA", font=(theme.UI_FAMILY, 12, "bold"),
                 background=theme.SURFACE, foreground=theme.BRAND).pack()

        # -- what it is built on ------------------------------------------
        tk.Label(outer, text="TECNOLOGIAS", font=theme.FONT_SMALL_BOLD,
                 background=theme.CANVAS, foreground=theme.INK_MUTED,
                 anchor="w").pack(fill="x", pady=(16, 6))

        table = tk.Frame(outer, background=theme.SURFACE, padx=16, pady=12,
                         highlightthickness=1, highlightbackground=theme.BORDER)
        table.pack(fill="both", expand=True)
        table.columnconfigure(1, weight=1)

        for row, (name, detail) in enumerate(self._stack()):
            tk.Label(table, text=name, font=theme.FONT_SMALL_BOLD, anchor="w",
                     background=theme.SURFACE, foreground=theme.INK).grid(
                row=row, column=0, sticky="w", pady=3, padx=(0, 14))
            tk.Label(table, text=detail, font=theme.FONT_SMALL, anchor="w",
                     background=theme.SURFACE, foreground=theme.INK_SOFT,
                     wraplength=340, justify="left").grid(row=row, column=1,
                                                          sticky="w", pady=3)

        buttons = tk.Frame(outer, background=theme.CANVAS)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Copiar informacoes", command=self._copy,
                   style="TButton").pack(side="left")
        ttk.Button(buttons, text="Fechar", command=self.destroy,
                   style="Accent.TButton").pack(side="right")

    def _stack(self) -> list[tuple[str, str]]:
        try:
            tk_version = self.tk.call("info", "patchlevel")
        except tk.TclError:  # pragma: no cover
            tk_version = tk.TkVersion
        return [
            ("Python", f"{platform.python_version()} ({platform.architecture()[0]})"),
            ("Interface", f"Tkinter / Tk {tk_version}"),
            ("Sistema", f"{platform.system()} {platform.release()}  -  build {platform.version()}"),
            ("Unidades opticas", "Win32 API via ctypes (deteccao, rotulo, serie, ejecao)"),
            ("Copia", "Por blocos, com retomada, verificacao e relatorio de falhas"),
            ("Trabalhos", "JSON atomico em %LOCALAPPDATA% e na pasta do lote"),
            ("Leitura de fotos", "Google Gemini (opcional, para importar o protocolo)"),
            ("CD de audio", "ffmpeg com libcdio (opcional, em desenvolvimento)"),
            ("Executavel", "Interpretado" if not getattr(sys, "frozen", False) else "Empacotado"),
        ]

    def _copy(self) -> None:
        lines = [f"{APP_TITLE} {__version__}"]
        lines += [f"{name}: {detail}" for name, detail in self._stack()]
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))


# -- the guides -----------------------------------------------------------
# (kind, text) blocks. "key" carries a (shortcut, meaning) pair instead.

TOPICS: dict[str, tuple[str, list]] = {
    "primeiros-passos": (
        "Primeiros passos",
        [
            ("p", "Do zero ate o primeiro disco copiado, em cinco passos."),
            ("step", "1.  Em Destino, clique em Procurar... e aponte a pasta onde o "
                     "backup sera gravado - normalmente uma pasta de rede."),
            ("step", "2.  Em Pasta do lote, escreva o nome da caixa ou do lote. "
                     "Ex.: CAIXA 02 (EG 1841 ao EG 1852)."),
            ("step", "3.  Clique em Criar trabalho. Abre uma janela para colar a lista "
                     "de pastas, um nome por linha, na ordem dos discos."),
            ("step", "4.  Insira o primeiro disco. Ele aparece no painel da direita com "
                     "uma contagem regressiva de 10 segundos."),
            ("step", "5.  Espere a contagem ou clique em Iniciar agora. Ao terminar, a "
                     "bandeja abre sozinha e a lista avanca para a proxima pasta."),
            ("note", "Tem a folha do protocolo de entrega em maos? Pule os passos 2 e 3 "
                     "e use Lote > Importar de fotos: o nome do lote e a lista de discos "
                     "saem da propria folha."),
        ],
    ),
    "ciclo-do-disco": (
        "O ciclo de um disco",
        [
            ("p", "O que acontece entre inserir o disco e poder trocar por outro."),
            ("bullet", "O disco e detectado e identificado: tipo, rotulo e numero de serie."),
            ("bullet", "O painel mostra para qual pasta ele vai e comeca a contagem "
                       "regressiva - a janela para intervir, se for o disco errado."),
            ("bullet", "A copia roda com barra de progresso, velocidade e tempo restante."),
            ("bullet", "No fim, a pasta e conferida, a linha vira concluida em verde e a "
                       "bandeja abre."),
            ("bullet", "Trocou o disco, tudo recomeca na proxima pasta pendente."),
            ("h2", "Enquanto a contagem corre"),
            ("bullet", "Iniciar agora nao espera os 10 segundos."),
            ("bullet", "Enviar para... manda este disco para qualquer outra pasta."),
            ("bullet", "Pular disco deixa a pasta pendente e ejeta."),
        ],
    ),
    "lista-de-pastas": (
        "Montando a lista de pastas",
        [
            ("p", "A lista e a fila: o proximo disco vai sempre para a primeira pasta "
                  "pendente, de cima para baixo."),
            ("bullet", "Adicionar pastas... aceita nomes colados, um por linha."),
            ("bullet", "Uma linha no formato EG 1841/Disco 01 cria a subpasta EG 1841 "
                       "com Disco 01 dentro."),
            ("bullet", "Duplo clique no nome renomeia - so funciona em pasta vazia."),
            ("bullet", "Alt+Cima e Alt+Baixo reordenam a pasta selecionada."),
            ("bullet", "Botao direito em qualquer linha abre tudo o que da para fazer "
                       "com ela."),
            ("note", "A pasta destacada em azul e a proxima da fila. A destacada em "
                     "amarelo esta acumulando: enquanto isso estiver ligado, todo disco "
                     "vai para ela, ignorando a ordem."),
        ],
    ),
    "varios-discos": (
        "Um conjunto com varios discos",
        [
            ("p", "Quando um mesmo item veio em tres, cinco ou dez discos e tudo "
                  "precisa cair na mesma pasta."),
            ("step", "1.  Clique com o botao direito na pasta de destino."),
            ("step", "2.  Escolha Acumular discos nesta pasta."),
            ("step", "3.  Insira os discos do conjunto, um apos o outro. Todos vao para "
                     "essa pasta, sem perguntar."),
            ("step", "4.  Terminado o conjunto, clique em Parar de acumular na faixa "
                     "amarela do topo."),
            ("p", "A faixa amarela fica visivel o tempo todo enquanto isso esta ligado, "
                  "e mostra quantos discos ja entraram."),
            ("note", "Nada e sobrescrito. Um arquivo com nome repetido e conteudo "
                     "diferente e gravado como arquivo (2).ext; um arquivo identico e "
                     "reconhecido e pulado."),
        ],
    ),
    "problemas": (
        "Quando algo da errado",
        [
            ("h2", "O disco nao le, ou trava no meio"),
            ("p", "Se a copia comeca a falhar, aparece um aviso vermelho no painel do "
                  "disco com o botao Disco defeituoso. Ele para a copia, mantem o que "
                  "ja foi gravado e escreve um relatorio dentro da pasta dizendo o que "
                  "nao pode ser lido."),
            ("h2", "O disco nem e reconhecido"),
            ("p", "Botao direito na linha da pasta > Marcar como pulada - disco "
                  "defeituoso... Da para anotar o motivo, e essa anotacao fica no "
                  "relatorio e no trabalho."),
            ("h2", "O disco veio fora de ordem"),
            ("p", "Use Enviar para... no painel do disco e escolha a pasta certa. As "
                  "pastas usadas recentemente aparecem no topo da lista."),
            ("h2", "A rede caiu no meio do lote"),
            ("p", "Nada se perde. Reabra o trabalho em Arquivo > Recentes: ele confere "
                  "o que existe em disco, pergunta o que fazer com o que ficou pela "
                  "metade e retoma de onde parou."),
        ],
    ),
    "encerrando": (
        "Encerrando o lote",
        [
            ("step", "1.  Confira a lista: toda linha deve estar concluida, pulada ou "
                     "com falha - nenhuma pendente."),
            ("step", "2.  Use Abrir pasta para conferir o resultado no Explorer."),
            ("step", "3.  Se a pasta vai ser entregue a outra pessoa, use "
                     "Arquivo > Limpar arquivos de controle para remover os arquivos "
                     "de bookkeeping do programa."),
            ("step", "4.  Clique em Fechar trabalho. A tela volta a ficar livre para o "
                     "proximo lote, com o Destino ja preenchido."),
            ("note", "Limpar arquivos de controle nunca toca nos dados copiados - "
                     "apenas na lista fixa de arquivos que o proprio programa criou."),
        ],
    ),
    "atalhos": (
        "Atalhos de teclado",
        [
            ("h2", "Trabalho"),
            ("key", ("Ctrl+N", "novo trabalho")),
            ("key", ("Ctrl+O", "abrir trabalho")),
            ("key", ("Ctrl+W", "fechar o trabalho aberto")),
            ("key", ("Ctrl+E", "abrir a pasta do lote no Explorer")),
            ("key", ("Ctrl+Q", "sair")),
            ("h2", "Lista de pastas"),
            ("key", ("Ctrl+Shift+A", "adicionar pastas")),
            ("key", ("Ctrl+I", "importar de fotos")),
            ("key", ("Alt+Cima / Alt+Baixo", "reordenar a pasta selecionada")),
            ("key", ("F2 ou duplo clique", "renomear a pasta")),
            ("key", ("Delete", "excluir a pasta da lista")),
            ("h2", "Disco"),
            ("key", ("Espaco", "iniciar a copia agora")),
            ("key", ("Esc", "cancelar a copia em andamento")),
            ("key", ("Ctrl+P", "pausar ou retomar")),
            ("key", ("Ctrl+J", "ejetar")),
            ("key", ("Ctrl+D", "enviar o disco para outra pasta")),
            ("h2", "Ajuda"),
            ("key", ("F1", "abrir esta ajuda")),
        ],
    ),
    "garantias": (
        "O que o programa nunca faz",
        [
            ("p", "Garantias, nao intencoes - cada uma tem teste automatizado."),
            ("bullet", "Nunca sobrescreve um arquivo existente com conteudo diferente."),
            ("bullet", "Nunca perde o que ja foi copiado ao pausar, pular, cancelar ou "
                       "marcar um disco como defeituoso."),
            ("bullet", "Nunca apaga dados seus: a limpeza mexe so na lista fixa de "
                       "arquivos do proprio programa, e nunca de forma recursiva."),
            ("bullet", "Nunca desiste do disco inteiro por causa de um arquivo ilegivel."),
            ("bullet", "Nunca marca como concluida uma pasta incompleta."),
            ("bullet", "Nunca grava sua chave de API dentro da pasta de backup."),
            ("bullet", "Nunca deixa o lote preso num disco que nao responde."),
        ],
    ),
}
