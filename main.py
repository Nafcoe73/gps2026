# -*- coding: utf-8 -*-
"""
Mapa Offline  -  parte 2 (Android, Kivy)
-----------------------------------------
100% offline: lê  mapa.png  +  rota.txt  e usa só o GPS do telemóvel.
  * destino   -> bolinha VERDE
  * eu (GPS)  -> bolinha VERMELHA (+ círculo da precisão), em tempo real
  * N / S / E / O nas margens (o mapa está sempre com o Norte para cima)
  * distância e direção até ao destino
  * zoom com dois dedos, arrastar com um dedo
  * caixa de navegação (em baixo): rua onde estás, próxima manobra e distância,
    alerta a laranja a menos de 100 m, vermelho se saíres da rota
  * com ruas.bin: sabe o nome da rua em qualquer sítio dentro do raio, mesmo fora da rota

Onde pôr os ficheiros (copiar do PC por cabo USB):
  Armazenamento interno / Android / data / org.rota.mapaoffline / files /
       mapa.png
       rota.txt
       ruas.bin   (opcional: todas as ruas do raio)
(a pasta é criada quando abres a app pela 1.ª vez; o caminho exato aparece no ecrã)
"""

import json
import math
import os
import struct
import sys
import threading
import zlib
from array import array

from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.graphics import Color, Ellipse, Line
from kivy.graphics.transformation import Matrix
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import ListProperty, StringProperty
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.image import Image
from kivy.uix.scatter import Scatter
from kivy.utils import platform

KV = """
<Caixa@Label>:
    color: 1, 1, 1, 1
    canvas.before:
        Color:
            rgba: 0, 0, 0, 0.62
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]

<Ponto@Caixa>:
    size_hint: None, None
    size: dp(36), dp(36)
    bold: True
    font_size: '18sp'

<Botao@Button>:
    font_size: '15sp'
    background_normal: ''
    background_color: 0.12, 0.12, 0.12, 0.88

<Ecra>:
    Caixa:
        id: info
        text: root.info
        size_hint: 0.96, None
        height: dp(92)
        pos_hint: {'center_x': 0.5, 'top': 0.985}
        halign: 'center'
        valign: 'middle'
        text_size: self.width - dp(12), None
        font_size: '14sp'
        markup: True

    Ponto:
        text: 'N'
        pos_hint: {'center_x': 0.5, 'top': 0.985 - (dp(100) / root.height if root.height else 0)}
    Ponto:
        text: 'S'
        pos_hint: {'center_x': 0.5}
        y: nav.top + dp(4)

    Label:
        id: nav
        text: root.nav
        markup: True
        color: 1, 1, 1, 1
        size_hint: 0.98, None
        height: max(dp(96), self.texture_size[1] + dp(14))
        pos_hint: {'center_x': 0.5, 'y': (dp(60) / root.height if root.height else 0)}
        halign: 'left'
        valign: 'middle'
        text_size: self.width - dp(16), None
        font_size: '16sp'
        canvas.before:
            Color:
                rgba: root.nav_cor
            RoundedRectangle:
                pos: self.pos
                size: self.size
                radius: [dp(10)]
    Ponto:
        text: 'E'
        pos_hint: {'right': 0.99, 'center_y': 0.5}
    Ponto:
        text: 'O'
        pos_hint: {'x': 0.01, 'center_y': 0.5}

    BoxLayout:
        size_hint: 0.98, None
        height: dp(52)
        pos_hint: {'center_x': 0.5, 'y': 0.005}
        spacing: dp(4)
        Botao:
            text: 'Onde estou'
            on_release: root.centrar_em_mim()
        Botao:
            text: 'Destino'
            on_release: root.centrar_no_destino()
        Botao:
            id: btn_seguir
            text: 'Seguir: não'
            on_release: root.alternar_seguir()
        Botao:
            text: 'Mapa todo'
            on_release: root.ver_tudo()
        Botao:
            text: 'Recarregar'
            on_release: root.carregar()
"""


# ---------------------------------------------------------------- geo (igual ao desktop)
def latlon_to_world(lat, lon, zoom):
    siny = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
    n = 256 * (2 ** zoom)
    return (n * (lon + 180.0) / 360.0,
            n * (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)))


def geo_to_px(meta, lat, lon):
    """Coordenadas -> pixel na imagem (origem no canto superior esquerdo)."""
    z, s = meta["zoom"], meta["scale"]
    cx, cy = latlon_to_world(meta["center_lat"], meta["center_lon"], z)
    x, y = latlon_to_world(lat, lon, z)
    return (x - cx) * s + meta["img_w"] / 2.0, (y - cy) * s + meta["img_h"] / 2.0


def metros_por_px(meta, lat):
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** meta["zoom"]) / meta["scale"]


def distancia(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def rumo(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def ponto_cardeal(graus):
    return ["N", "NE", "E", "SE", "S", "SO", "O", "NO"][int((graus + 22.5) // 45) % 8]


def fmt_dist(m):
    return f"{m:.0f} m" if m < 1000 else f"{m / 1000:.2f} km"


def decode_polyline(s, precisao=5):
    pts, i, lat, lng = [], 0, 0, 0
    f = 10 ** precisao
    while i < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lng += vals[1]
        pts.append((lat / f, lng / f))
    return pts


def proj_segmento(px, py, x1, y1, x2, y2):
    """Distância do ponto ao segmento e fração t (0..1) da projeção."""
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    qx, qy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - qx, py - qy), t


class BaseRuas:
    """Lê ruas.bin (gerado no PC) e diz qual a rua com nome mais próxima."""

    def __init__(self, caminho):
        with open(caminho, "rb") as f:
            data = f.read()
        if data[:6] != b"RUAS1\n":
            raise ValueError("ruas.bin inválido")
        n = struct.unpack("<I", data[6:10])[0]
        cab = json.loads(data[10:10 + n].decode("utf-8"))
        pos = 10 + n
        for nome, tam in cab["blocos"]:
            raw = zlib.decompress(data[pos:pos + tam])
            pos += tam
            if nome == "nomes":
                self.nomes = raw.decode("utf-8").split("\n")
            else:
                a = array("i")
                a.frombytes(raw)
                if sys.byteorder == "big":
                    a.byteswap()
                setattr(self, nome, a)
        for k in ("lat0", "lon0", "kx", "ky", "cel", "nx", "ny", "xmin", "ymin"):
            setattr(self, k, cab[k])

    def procurar(self, lat, lon, max_d=150.0):
        """-> (nome da rua mais próxima, distância em m) ou (None, None)"""
        cand = self.candidatos(lat, lon, max_d)
        if not cand:
            return None, None
        nome = min(cand, key=cand.get)
        return nome, cand[nome]

    def candidatos(self, lat, lon, max_d=150.0):
        """-> {nome: distância mínima} de todas as ruas até max_d metros"""
        x = (lon - self.lon0) * self.kx
        y = (lat - self.lat0) * self.ky
        ci = int((x - self.xmin) // self.cel)
        cj = int((y - self.ymin) // self.cel)
        alcance = int(max_d // self.cel) + 1
        cand = {}
        px, py, seg, goff, gseg, nomes = self.px, self.py, self.seg, self.goff, self.gseg, self.nomes
        for j in range(cj - alcance, cj + alcance + 1):
            if not 0 <= j < self.ny:
                continue
            for i in range(ci - alcance, ci + alcance + 1):
                if not 0 <= i < self.nx:
                    continue
                c = j * self.nx + i
                for k in range(goff[c], goff[c + 1]):
                    s = gseg[k]
                    d, _ = proj_segmento(x, y, px[s], py[s], px[s + 1], py[s + 1])
                    if d <= max_d:
                        n = nomes[seg[s]]
                        if d < cand.get(n, 1e18):
                            cand[n] = d
        return cand


class Navegacao:
    """Traçado completo da rota + passos (do PC, dentro do mapa.png)."""

    def __init__(self, nav):
        geo = decode_polyline(nav.get("poly", ""))
        if len(geo) < 2:
            raise ValueError("rota sem traçado")
        self.lat0, self.lon0 = geo[0]
        self.kx = 111320.0 * math.cos(math.radians(self.lat0))
        self.ky = 111320.0
        self.xy = [self._xy(la, lo) for la, lo in geo]
        self.cum = [0.0]
        for (x1, y1), (x2, y2) in zip(self.xy, self.xy[1:]):
            self.cum.append(self.cum[-1] + math.hypot(x2 - x1, y2 - y1))
        self.total = self.cum[-1]
        self.passos, self.along = [], []
        ini = 0
        for p in nav.get("passos", []):
            x, y = self._xy(p["la"], p["lo"])
            melhor, bi = None, ini
            for i in range(ini, len(self.xy)):           # passos seguem a ordem da rota
                d = (self.xy[i][0] - x) ** 2 + (self.xy[i][1] - y) ** 2
                if melhor is None or d < melhor:
                    melhor, bi = d, i
            ini = bi
            self.passos.append(p)
            self.along.append(self.cum[bi])

    def _xy(self, lat, lon):
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def estado(self, lat, lon):
        """-> (distância já feita na rota, distância ao caminho, índice do passo atual)"""
        x, y = self._xy(lat, lon)
        melhor, ao_longo = None, 0.0
        for i in range(len(self.xy) - 1):
            (x1, y1), (x2, y2) = self.xy[i], self.xy[i + 1]
            d, t = proj_segmento(x, y, x1, y1, x2, y2)
            if melhor is None or d < melhor:
                melhor, ao_longo = d, self.cum[i] + t * (self.cum[i + 1] - self.cum[i])
        atual = 0
        for k, a in enumerate(self.along):
            if a <= ao_longo + 10:
                atual = k
        return ao_longo, melhor, atual


# ---------------------------------------------------------------- ficheiros
def ler_texto_png(caminho):
    """Lê os metadados de texto (tEXt / iTXt / zTXt) de um PNG, sem bibliotecas extra."""
    out = {}
    with open(caminho, "rb") as f:
        if f.read(8) != b"\x89PNG\r\n\x1a\n":
            return out
        while True:
            cab = f.read(8)
            if len(cab) < 8:
                break
            tam, tipo = struct.unpack(">I4s", cab)
            if tipo == b"IDAT":          # dados da imagem: saltar
                f.seek(tam + 4, 1)
                continue
            dados = f.read(tam)
            f.read(4)                    # CRC
            if tipo == b"tEXt":
                k, _, v = dados.partition(b"\x00")
                out[k.decode("latin-1")] = v.decode("latin-1")
            elif tipo == b"zTXt":
                k, _, v = dados.partition(b"\x00")
                out[k.decode("latin-1")] = zlib.decompress(v[1:]).decode("latin-1")
            elif tipo == b"iTXt":
                k, _, resto = dados.partition(b"\x00")
                comprimido = resto[0]
                resto = resto[2:]
                _, _, resto = resto.partition(b"\x00")   # língua
                _, _, txt = resto.partition(b"\x00")     # palavra traduzida
                if comprimido:
                    txt = zlib.decompress(txt)
                out[k.decode("latin-1")] = txt.decode("utf-8")
            elif tipo == b"IEND":
                break
    return out


def ler_rota_txt(caminho):
    """Linhas 'local | lat , lon'  ->  [(nome, lat, lon), ...]"""
    pts = []
    with open(caminho, encoding="utf-8", errors="replace") as f:
        for linha in f:
            if "|" not in linha:
                continue
            nome, _, coords = linha.rpartition("|")
            try:
                a, b = coords.split(",")
                pts.append((nome.strip(), float(a), float(b)))
            except ValueError:
                continue                 # cabeçalho "local | coordenadas"
    return pts


# ---------------------------------------------------------------- ecrã principal
COR_ESPERA = [0, 0, 0, 0.62]
COR_NORMAL = [0.05, 0.27, 0.55, 0.88]
COR_ALERTA = [0.96, 0.52, 0.0, 0.94]
COR_FORA = [0.78, 0.1, 0.1, 0.92]
ALERTA_M = 100      # distância para o aviso laranja


class Ecra(FloatLayout):
    info = StringProperty("A arrancar…")
    nav = StringProperty("À espera do GPS…")
    nav_cor = ListProperty(COR_ESPERA)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.meta = None
        self.destino = None              # (nome, lat, lon)
        self.pontos = []
        self.ruas = None                 # BaseRuas (ruas.bin)
        self.navegacao = None            # Navegacao (traçado da rota)
        self.estado_ruas = ""
        self.eu = None                   # dict com lat, lon, accuracy, speed, bearing
        self.seguir = False
        self.gps_estado = "GPS a iniciar…"
        self.pasta = None

        self.scatter = Scatter(do_rotation=False, scale_min=1, scale_max=16,
                               auto_bring_to_front=False, size_hint=(None, None))
        self.img = Image(fit_mode="contain", nocache=True, size_hint=(None, None), pos=(0, 0))
        self.scatter.add_widget(self.img)
        self.add_widget(self.scatter, index=len(self.children))   # fica por baixo dos botões

        self.bind(size=self._redimensionar)
        self.scatter.bind(scale=self.desenhar, pos=self.desenhar)
        self.img.bind(texture_size=self.desenhar, size=self.desenhar)

    # ---------- layout / transformações
    def _redimensionar(self, *a):
        self.scatter.size = self.size
        self.img.size = self.size
        self.ver_tudo()

    def ver_tudo(self, *a):
        self.scatter.transform = Matrix()
        self.scatter.pos = self.pos
        self.desenhar()

    def _rect_imagem(self):
        """(ox, oy, k): canto inferior esquerdo da imagem visível e px_ecrã / px_imagem."""
        nw, nh = self.img.norm_image_size
        k = nw / float(self.meta["img_w"])
        return self.img.center_x - nw / 2.0, self.img.center_y - nh / 2.0, k

    def para_local(self, lat, lon):
        px, py = geo_to_px(self.meta, lat, lon)
        ox, oy, k = self._rect_imagem()
        return ox + px * k, oy + (self.meta["img_h"] - py) * k, px, py

    def _centrar(self, lat, lon):
        if not self.meta:
            return
        if self.scatter.scale < 2.5:
            self.scatter.scale = 3
        x, y, _, _ = self.para_local(lat, lon)
        s = self.scatter.scale
        self.scatter.pos = (self.center_x - x * s, self.center_y - y * s)
        self.desenhar()

    def centrar_em_mim(self):
        if self.eu:
            self._centrar(self.eu["lat"], self.eu["lon"])
        else:
            self.atualizar_info("Ainda sem posição GPS.")

    def centrar_no_destino(self):
        if self.destino:
            self._centrar(self.destino[1], self.destino[2])

    def alternar_seguir(self):
        self.seguir = not self.seguir
        self.ids.btn_seguir.text = "Seguir: sim" if self.seguir else "Seguir: não"
        if self.seguir:
            self.centrar_em_mim()

    # ---------- ficheiros
    def pastas_procura(self):
        pastas = []
        if platform == "android":
            try:
                from jnius import autoclass
                act = autoclass("org.kivy.android.PythonActivity").mActivity
                ext = act.getExternalFilesDir(None)     # cria a pasta se não existir
                if ext is not None:
                    pastas.append(ext.getAbsolutePath())
            except Exception:
                pass
            pastas += ["/storage/emulated/0/Download/rota", "/storage/emulated/0/Download"]
        pastas.append(os.path.dirname(os.path.abspath(__file__)))  # ficheiros dentro do APK
        return pastas

    def carregar(self, *a):
        self.meta, self.destino, self.pontos = None, None, []
        self.ruas, self.navegacao, self.estado_ruas = None, None, ""
        pastas = self.pastas_procura()
        for p in pastas:
            png = os.path.join(p, "mapa.png")
            if os.path.isfile(png):
                try:
                    textos = ler_texto_png(png)
                    meta = json.loads(textos.get("rota_geo", ""))
                except (OSError, ValueError):
                    continue
                self.pasta, self.meta = p, meta
                try:
                    if textos.get("rota_nav"):
                        self.navegacao = Navegacao(json.loads(textos["rota_nav"]))
                except (ValueError, KeyError):
                    self.navegacao = None
                self.img.source = png
                self.img.reload()
                break
        if not self.meta:
            self.info = ("[b]Não encontrei mapa.png[/b] (gerado pela app do PC).\n"
                         f"Copia mapa.png e rota.txt para:\n[size=12sp]{pastas[0]}[/size]")
            self.desenhar()
            return

        txt = os.path.join(self.pasta, "rota.txt")
        if os.path.isfile(txt):
            self.pontos = ler_rota_txt(txt)
        dest = [p for p in self.pontos if p[0].upper().startswith("[DESTINO]")]
        if dest:
            nome, lat, lon = dest[-1]
            self.destino = (nome.replace("[DESTINO]", "").strip(), lat, lon)
        elif self.pontos:
            self.destino = self.pontos[-1]
        else:
            self.destino = (self.meta.get("dest_nome", "Destino"), self.meta["dest_lat"], self.meta["dest_lon"])
        self.ver_tudo()
        self.atualizar_info()
        f_ruas = os.path.join(self.pasta, "ruas.bin")
        if os.path.isfile(f_ruas):
            self.estado_ruas = "A carregar as ruas do raio…"
            threading.Thread(target=self._carregar_ruas, args=(f_ruas,), daemon=True).start()
        self.atualizar_nav()

    def _carregar_ruas(self, caminho):
        try:
            base = BaseRuas(caminho)
            self._ruas_prontas(base, f"{len(base.nomes)} ruas do raio carregadas")
        except Exception as e:  # noqa: BLE001
            self._ruas_prontas(None, f"Erro a ler ruas.bin: {e}")

    @mainthread
    def _ruas_prontas(self, base, msg):
        self.ruas, self.estado_ruas = base, msg
        self.atualizar_nav()

    # ---------- GPS
    def iniciar_gps(self):
        if platform != "android":
            self.gps_estado = "Sem GPS (não é Android)"
            self.atualizar_info()
            return
        from android.permissions import Permission, request_permissions

        def resposta(perms, ok):
            if any(ok):
                Clock.schedule_once(lambda dt: self._ligar_gps(), 0)
            else:
                self.gps_estado = "Permissão de localização recusada"
                self.atualizar_info()

        request_permissions([Permission.ACCESS_FINE_LOCATION, Permission.ACCESS_COARSE_LOCATION], resposta)

    def _ligar_gps(self):
        try:
            from plyer import gps
            gps.configure(on_location=self.ao_mudar_posicao, on_status=self.ao_mudar_estado)
            gps.start(minTime=1000, minDistance=0)
            self.gps_estado = "À procura de satélites… (ao ar livre)"
        except Exception as e:  # noqa: BLE001
            self.gps_estado = f"Erro GPS: {e}"
        self.atualizar_info()

    @mainthread
    def ao_mudar_posicao(self, **kw):
        try:
            self.eu = {"lat": float(kw["lat"]), "lon": float(kw["lon"]),
                       "accuracy": float(kw.get("accuracy") or 0),
                       "speed": float(kw.get("speed") or 0),
                       "bearing": float(kw.get("bearing") or 0)}
        except (KeyError, TypeError, ValueError):
            return
        self.gps_estado = "GPS ok"
        if self.seguir:
            self._centrar(self.eu["lat"], self.eu["lon"])
        self.desenhar()
        self.atualizar_info()
        self.atualizar_nav()

    @mainthread
    def ao_mudar_estado(self, stype, status):
        if stype == "provider-disabled":
            self.gps_estado = "Liga a Localização (GPS) nas definições!"
        elif stype == "provider-enabled":
            self.gps_estado = "GPS ligado, à procura de sinal…"
        self.atualizar_info()

    # ---------- texto
    def atualizar_info(self, extra=None):
        if not self.meta:
            return
        linhas = []
        if self.destino:
            linhas.append(f"[color=55ee66]●[/color] [b]{self.destino[0][:60]}[/b]")
        if self.eu:
            d = distancia((self.eu["lat"], self.eu["lon"]), self.destino[1:])
            r = rumo((self.eu["lat"], self.eu["lon"]), self.destino[1:])
            linhas.append(f"[color=ff4444]●[/color] Destino a [b]{fmt_dist(d)}[/b] para "
                          f"[b]{ponto_cardeal(r)}[/b] ({r:.0f}°)   precisão ±{self.eu['accuracy']:.0f} m")
            linhas.append(f"{self.eu['lat']:.6f}, {self.eu['lon']:.6f}   ·   {self.eu['speed'] * 3.6:.0f} km/h")
            px, py = geo_to_px(self.meta, self.eu["lat"], self.eu["lon"])
            if not (0 <= px <= self.meta["img_w"] and 0 <= py <= self.meta["img_h"]):
                linhas.append("[color=ffaa00]Estás fora do mapa (seta vermelha na borda).[/color]")
        else:
            linhas.append(self.gps_estado)
        if extra:
            linhas.append(extra)
        self.info = "\n".join(linhas)

    # ---------- caixa de navegação
    def _rua_aqui(self, lat, lon, precisao, preferida=None):
        """Nome da rua onde estou (base de ruas). -> (texto, exato?)
        'preferida' = rua da rota; ganha nos cruzamentos se também estiver perto."""
        if not self.ruas:
            return None, False
        cand = self.ruas.candidatos(lat, lon, 150)
        if not cand:
            return None, False
        limite = max(25.0, min(precisao, 50.0))
        if preferida and cand.get(preferida, 1e18) <= limite:
            return preferida, True
        nome = min(cand, key=cand.get)
        d = cand[nome]
        if d <= limite:
            return nome, True
        return f"perto de {nome} ({d:.0f} m)", False

    def atualizar_nav(self):
        if not self.meta:
            self.nav, self.nav_cor = "", COR_ESPERA
            return
        if not self.eu:
            extra = f"\n[size=13sp]{self.estado_ruas}[/size]" if self.estado_ruas else ""
            self.nav, self.nav_cor = "À espera do GPS…" + extra, COR_ESPERA
            return
        lat, lon, prec = self.eu["lat"], self.eu["lon"], self.eu["accuracy"]

        if not self.navegacao:
            rua, exata = self._rua_aqui(lat, lon, prec)
            self.nav_cor = COR_NORMAL
            self.nav = f"[b]Estás em:[/b] {rua or 'rua desconhecida'}"
            if not self.ruas:
                self.nav += "\n[size=13sp](sem ruas.bin: só sei os nomes da rota)[/size]"
            return

        nv = self.navegacao
        feito, fora, i = nv.estado(lat, lon)
        limite = max(50.0, min(prec * 1.5, 120.0))
        rua, exata = self._rua_aqui(lat, lon, prec, None if fora > limite else nv.passos[i].get("n"))
        if fora > limite:
            d = distancia((lat, lon), self.destino[1:])
            r = rumo((lat, lon), self.destino[1:])
            self.nav_cor = COR_FORA
            self.nav = (f"[b]FORA DA ROTA[/b] (a {fmt_dist(fora)} do caminho)\n"
                        f"[b]Estás em:[/b] {rua or 'rua desconhecida'}\n"
                        f"Destino a {fmt_dist(d)} para {ponto_cardeal(r)}")
            return

        passo = nv.passos[i]
        aqui = rua if (rua and exata) else (passo.get("n") or rua or "—")
        linhas = [f"[b]Estás em:[/b] {aqui}"]
        cor = COR_NORMAL
        if i + 1 < len(nv.passos):
            prox = nv.passos[i + 1]
            falta = max(0.0, nv.along[i + 1] - feito)
            if falta <= ALERTA_M:
                cor = COR_ALERTA
                linhas.append(f"[b][size=19sp]DAQUI A {fmt_dist(falta).upper()}: {prox['t']}[/size][/b]")
            else:
                linhas.append(f"[b]A seguir[/b] ({fmt_dist(falta)}): {prox['t']}")
        linhas.append(f"Faltam {fmt_dist(max(0.0, nv.total - feito))} pela rota")
        self.nav, self.nav_cor = "\n".join(linhas), cor

    # ---------- desenho
    def desenhar(self, *a):
        c = self.img.canvas.after
        c.clear()
        if not self.meta or not self.img.texture:
            return
        s = self.scatter.scale or 1
        r = dp(9) / s
        lw = max(1.0, dp(2) / s)
        W, H = self.meta["img_w"], self.meta["img_h"]
        ox, oy, k = self._rect_imagem()
        with c:
            # pontos da rota (pequenos, azuis)
            Color(0.1, 0.4, 0.9, 0.9)
            for nome, lat, lon in self.pontos:
                x, y, _, _ = self.para_local(lat, lon)
                Ellipse(pos=(x - r * 0.35, y - r * 0.35), size=(r * 0.7, r * 0.7))

            # destino: VERDE
            if self.destino:
                x, y, _, _ = self.para_local(self.destino[1], self.destino[2])
                Color(1, 1, 1, 1)
                Ellipse(pos=(x - r - lw, y - r - lw), size=(2 * (r + lw), 2 * (r + lw)))
                Color(0.05, 0.75, 0.2, 1)
                Ellipse(pos=(x - r, y - r), size=(2 * r, 2 * r))

            # eu: VERMELHO
            if self.eu:
                x, y, px, py = self.para_local(self.eu["lat"], self.eu["lon"])
                dentro = 0 <= px <= W and 0 <= py <= H
                if dentro:
                    raio_prec = self.eu["accuracy"] / metros_por_px(self.meta, self.eu["lat"]) * k
                    if raio_prec > r:
                        Color(1, 0, 0, 0.15)
                        Ellipse(pos=(x - raio_prec, y - raio_prec), size=(2 * raio_prec, 2 * raio_prec))
                    Color(1, 1, 1, 1)
                    Ellipse(pos=(x - r - lw, y - r - lw), size=(2 * (r + lw), 2 * (r + lw)))
                    Color(0.9, 0.05, 0.05, 1)
                    Ellipse(pos=(x - r, y - r), size=(2 * r, 2 * r))
                    if self.eu["speed"] > 0.7:          # seta da direção do movimento
                        b = math.radians(self.eu["bearing"])
                        Line(points=[x, y, x + math.sin(b) * r * 3, y + math.cos(b) * r * 3], width=lw * 1.5)
                else:
                    # fora do mapa: anel vermelho na borda + seta a apontar para onde estás
                    cx = ox + min(max(px, 0), W) * k
                    cy = oy + (H - min(max(py, 0), H)) * k
                    ang = math.atan2(y - cy, x - cx)
                    Color(0.9, 0.05, 0.05, 1)
                    Line(circle=(cx, cy, r), width=lw * 1.5)
                    Line(points=[cx, cy, cx + math.cos(ang) * r * 2.5, cy + math.sin(ang) * r * 2.5], width=lw * 2)


class MapaOfflineApp(App):
    title = "Mapa Offline"

    def build(self):
        Builder.load_string(KV)
        self.ecra = Ecra()
        Clock.schedule_once(lambda dt: self.ecra.carregar(), 0.3)
        Clock.schedule_once(lambda dt: self.ecra.iniciar_gps(), 0.6)
        return self.ecra

    def on_pause(self):
        return True      # continua vivo em segundo plano

    def on_stop(self):
        if platform == "android":
            try:
                from plyer import gps
                gps.stop()
            except Exception:
                pass


if __name__ == "__main__":
    MapaOfflineApp().run()
