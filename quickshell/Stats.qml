import Quickshell
import Quickshell.Io
import QtQuick

Scope {
    id: root

    property string tempPath: ""
    property var d: defaults()
    property var st: ({
        n: 0,
        prevTot: {},
        prevIdle: {},
        netLast: null,
        cpuHist: zeros(61),
        downHist: zeros(61),
        upHist: zeros(61),
        model: "",
        freq: "—",
        temp: "—",
        procs: [],
        disks: [],
        media: mediaOff()
    })

    function zeros(k) {
        var a = []
        for (var i = 0; i < k; i++) a.push(0)
        return a
    }

    function mediaOff() {
        return { on: false, playing: false, title: "", sub: "", frac: 0, time: "", note: "nothing playing" }
    }

    function defaults() {
        return {
            clockH: "00", clockM: "00", clockS: "00", day: "", date: "", who: "", uptime: "",
            cpu: 0, cpuText: "0%", cpuHist: [], cores: [], model: "", freq: "—", temp: "—", load: "load 0.00",
            ram: 0, ramPct: "—", ramText: "", swap: 0, swapText: "", disks: [],
            down: "0B/s", up: "0B/s", downTotal: "", upTotal: "", downHist: [], upHist: [],
            procs: [], media: mediaOff()
        }
    }

    function pad(v) { return v < 10 ? "0" + v : "" + v }

    function txt(v) {
        try { return v.text() || "" } catch (e) { return "" }
    }

    function human(n) {
        if (n < 1024) return n.toFixed(0) + "B"
        var units = ["K", "M", "G", "T"]
        for (var i = 0; i < 4; i++) {
            n /= 1024
            if (n < 1024 || i === 3) return n.toFixed(1) + units[i]
        }
        return ""
    }

    function mmss(us) {
        var s = Math.floor(us / 1000000)
        return Math.floor(s / 60) + ":" + pad(s % 60)
    }

    function norm(hist, floor) {
        var hi = floor
        for (var i = 0; i < hist.length; i++) if (hist[i] > hi) hi = hist[i]
        return hist.map(function (v) { return v / hi })
    }

    function kb(text, key) {
        var m = text.match(new RegExp(key + ":\\s+(\\d+)"))
        return m ? Number(m[1]) * 1024 : 0
    }

    function parseCpu(text) {
        var lines = text.split("\n"), cores = [], all = 0
        for (var i = 0; i < lines.length; i++) {
            var l = lines[i]
            if (l.indexOf("cpu") !== 0) continue
            var p = l.trim().split(/\s+/), name = p[0], tot = 0, idle = 0
            for (var j = 1; j <= 8 && j < p.length; j++) {
                var v = Number(p[j])
                tot += v
                if (j === 4 || j === 5) idle += v
            }
            var pt = st.prevTot[name], pi = st.prevIdle[name], pct = 0
            if (pt !== undefined && tot > pt) pct = 1 - (idle - pi) / (tot - pt)
            st.prevTot[name] = tot
            st.prevIdle[name] = idle
            pct = Math.max(0, Math.min(1, pct))
            if (name === "cpu") all = pct
            else cores.push(pct)
        }
        return { all: all, cores: cores }
    }

    function parseNet(text) {
        var rx = 0, tx = 0, lines = text.split("\n")
        for (var i = 2; i < lines.length; i++) {
            var l = lines[i], c = l.indexOf(":")
            if (c < 0) continue
            var name = l.slice(0, c).trim()
            if (name === "lo" || /^(docker|veth|br-|virbr)/.test(name)) continue
            var f = l.slice(c + 1).trim().split(/\s+/)
            rx += Number(f[0])
            tx += Number(f[8])
        }
        return [rx, tx]
    }

    function parseTop(text) {
        var lines = text.split("\n"), start = 0, i
        for (i = lines.length - 1; i >= 0; i--) {
            if (lines[i].indexOf("top - ") === 0) { start = i; break }
        }
        var agg = {}
        for (i = start; i < lines.length; i++) {
            var p = lines[i].trim().split(/\s+/)
            if (p.length < 12 || !/^\d+$/.test(p[0])) continue
            var name = p[11]
            var a = agg[name] || (agg[name] = [0, 0, 0])
            a[0] += Number(p[8])
            a[1] += Number(p[9])
            a[2]++
        }
        var keys = Object.keys(agg).sort(function (x, y) { return (agg[y][0] - agg[x][0]) || (agg[y][1] - agg[x][1]) })
        return keys.map(function (k) {
            var a = agg[k]
            return {
                name: a[2] > 1 ? k + " ×" + a[2] : k,
                cpu: Math.round(a[0]) + "%",
                mem: a[1].toFixed(1) + "%",
                frac: Math.min(a[0], 100) / 100
            }
        })
    }

    function parseDf(out) {
        var lines = out.trim().split("\n").slice(1), seen = {}, res = []
        for (var i = 0; i < lines.length && res.length < 2; i++) {
            var m = lines[i].match(/^(\S+)\s+(\d+)\s+(\d+)\s+(.*)$/)
            if (!m || seen[m[1]]) continue
            var mp = m[4], size = Number(m[2]), used = Number(m[3])
            if (!size) continue
            if (/^\/(boot|snap|var\/lib\/docker|var\/lib\/containers)/.test(mp)) continue
            if (mp.indexOf("/run") === 0 && mp.indexOf("/run/media") !== 0) continue
            seen[m[1]] = true
            res.push({
                name: mp.length <= 14 ? mp : "…" + mp.slice(-13),
                frac: used / size,
                text: human(used) + " / " + human(size)
            })
        }
        return res
    }

    function parseMedia(out) {
        out = out.trim()
        if (!out) return mediaOff()
        var p = out.split("\t")
        while (p.length < 6) p.push("")
        var pos = p[4], len = p[5], frac = 0, tm = ""
        if (/^\d+$/.test(pos) && /^\d+$/.test(len) && Number(len) > 0) {
            frac = Number(pos) / Number(len)
            tm = mmss(Number(pos)) + " / " + mmss(Number(len))
        }
        return {
            on: true,
            playing: p[0] === "Playing",
            title: p[3] || "Unknown",
            sub: (p[2] || "—") + "  ·  " + p[1],
            frac: frac,
            time: tm,
            note: ""
        }
    }

    function tick() {
        st.n++
        fStat.reload()
        fMem.reload()
        fNet.reload()
        fLoad.reload()
        fUp.reload()
        if (st.n % 3 === 1) {
            fCpuinfo.reload()
            fTemp.reload()
            if (!topProc.running) topProc.running = true
        }
        if (st.n % 10 === 1 && !dfProc.running) dfProc.running = true
        if (st.n % 2 === 0 && !mediaProc.running) mediaProc.running = true
        pub.restart()
    }

    function publish() {
        var now = new Date()

        var cpu = parseCpu(txt(fStat))
        st.cpuHist.push(cpu.all)
        st.cpuHist.shift()

        if (st.n % 3 === 1) {
            var ci = txt(fCpuinfo)
            if (ci) {
                if (!st.model) {
                    var mm = ci.match(/^model name\s*:\s*(.+)$/m)
                    if (mm) st.model = mm[1]
                }
                var re = /^cpu MHz\s*:\s*([\d.]+)/gm, hit, sum = 0, cnt = 0
                while ((hit = re.exec(ci)) !== null) { sum += Number(hit[1]); cnt++ }
                st.freq = cnt ? (sum / cnt / 1000).toFixed(2) + " GHz" : "—"
            }
            var tv = txt(fTemp)
            st.temp = tv ? Math.round(Number(tv) / 1000) + "°C" : "—"
        }

        var mem = txt(fMem)
        var total = kb(mem, "MemTotal"), avail = kb(mem, "MemAvailable")
        var sTot = kb(mem, "SwapTotal"), sFree = kb(mem, "SwapFree")
        var used = total - avail, sUsed = sTot - sFree

        var nn = parseNet(txt(fNet)), t = Date.now(), down = 0, up = 0
        if (st.netLast) {
            var dt = Math.max((t - st.netLast[0]) / 1000, 0.001)
            down = Math.max(nn[0] - st.netLast[1], 0) / dt
            up = Math.max(nn[1] - st.netLast[2], 0) / dt
        }
        st.netLast = [t, nn[0], nn[1]]
        st.downHist.push(down)
        st.downHist.shift()
        st.upHist.push(up)
        st.upHist.shift()

        var secs = Math.floor(Number(txt(fUp).split(" ")[0]) || 0)
        var days = Math.floor(secs / 86400), hrs = Math.floor((secs % 86400) / 3600), mins = Math.floor((secs % 3600) / 60)
        var uptime = (days ? days + "d " : "") + hrs + "h " + pad(mins) + "m"

        var la = Number(txt(fLoad).split(" ")[0]) || 0

        root.d = {
            clockH: Qt.formatDateTime(now, "HH"),
            clockM: Qt.formatDateTime(now, "mm"),
            clockS: Qt.formatDateTime(now, "ss"),
            day: Qt.formatDateTime(now, "dddd"),
            date: Qt.formatDateTime(now, "dd MMMM yyyy"),
            who: Quickshell.env("USER") + "@" + txt(fHost).trim(),
            uptime: uptime,
            cpu: cpu.all,
            cpuText: Math.round(cpu.all * 100) + "%",
            cpuHist: st.cpuHist.slice(),
            cores: cpu.cores,
            model: st.model,
            freq: st.freq,
            temp: st.temp,
            load: "load " + la.toFixed(2),
            ram: total ? used / total : 0,
            ramPct: total ? Math.round(used / total * 100) + "%" : "—",
            ramText: human(used) + " / " + human(total),
            swap: sTot ? sUsed / sTot : 0,
            swapText: human(sUsed) + " / " + human(sTot),
            disks: st.disks,
            down: human(down) + "/s",
            up: human(up) + "/s",
            downTotal: human(nn[0]) + " since boot",
            upTotal: human(nn[1]) + " since boot",
            downHist: norm(st.downHist, 64 * 1024),
            upHist: norm(st.upHist, 64 * 1024),
            procs: st.procs,
            media: st.media
        }
    }

    FileView { id: fStat; path: "/proc/stat" }
    FileView { id: fMem; path: "/proc/meminfo" }
    FileView { id: fNet; path: "/proc/net/dev" }
    FileView { id: fLoad; path: "/proc/loadavg" }
    FileView { id: fUp; path: "/proc/uptime" }
    FileView { id: fCpuinfo; path: "/proc/cpuinfo" }
    FileView { id: fHost; path: "/proc/sys/kernel/hostname" }
    FileView { id: fTemp; path: root.tempPath }

    Process {
        running: true
        command: ["sh", "-c", 'for n in k10temp coretemp zenpower cpu_thermal acpitz; do for d in /sys/class/hwmon/hwmon*; do [ "$(cat $d/name 2>/dev/null)" = "$n" ] && echo "$d/temp1_input" && exit; done; done']
        stdout: StdioCollector {
            onStreamFinished: root.tempPath = text.trim()
        }
    }

    Process {
        id: topProc
        command: ["env", "LC_ALL=C", "top", "-b", "-n", "2", "-d", "0.5", "-o", "%CPU"]
        stdout: StdioCollector {
            onStreamFinished: root.st.procs = root.parseTop(text)
        }
    }

    Process {
        id: dfProc
        command: ["df", "-B1", "--output=source,size,used,target", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs", "-x", "overlay", "-x", "efivarfs", "-x", "vfat"]
        stdout: StdioCollector {
            onStreamFinished: root.st.disks = root.parseDf(text)
        }
    }

    Process {
        id: mediaProc
        command: ["playerctl", "metadata", "--format", "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}\t{{position}}\t{{mpris:length}}"]
        stdout: StdioCollector {
            onStreamFinished: root.st.media = root.parseMedia(text)
        }
    }

    Timer {
        interval: 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.tick()
    }

    Timer {
        interval: 350
        running: true
        onTriggered: root.tick()
    }

    Timer {
        id: pub
        interval: 150
        onTriggered: root.publish()
    }
}
