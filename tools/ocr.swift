// Vision OCR helper: prints one JSON object per recognized token (pixel coords, top-left origin).
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count >= 2, let img = NSImage(contentsOfFile: args[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("usage: ocr <image>\n".data(using: .utf8)!); exit(1)
}
let W = CGFloat(cg.width), H = CGFloat(cg.height)
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
req.minimumTextHeight = 0.004
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try handler.perform([req])
var out: [[String: Any]] = []
for obs in req.results ?? [] {
    guard let cand = obs.topCandidates(1).first else { continue }
    let s = cand.string
    var tokens: [(Range<String.Index>)] = []
    var i = s.startIndex
    while i < s.endIndex {
        while i < s.endIndex && s[i] == " " { i = s.index(after: i) }
        if i >= s.endIndex { break }
        var j = i
        while j < s.endIndex && s[j] != " " { j = s.index(after: j) }
        tokens.append(i..<j); i = j
    }
    for r in tokens {
        guard let bb = try? cand.boundingBox(for: r)?.boundingBox else { continue }
        out.append(["t": String(s[r]), "x": Double(bb.minX * W), "y": Double((1 - bb.maxY) * H),
                    "w": Double(bb.width * W), "h": Double(bb.height * H), "c": Double(cand.confidence),
                    "line": String(s)])
    }
}
let data = try JSONSerialization.data(withJSONObject: out, options: [])
FileHandle.standardOutput.write(data)
