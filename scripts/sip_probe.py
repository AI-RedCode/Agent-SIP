#!/usr/bin/env python3
"""Probe a SIP registrar and outbound INVITE on one UDP socket."""

import argparse
import secrets
import socket

from app.sip import digest_authorization, parse_message


def request(method, uri, headers, body=""):
    lines = [f"{method} {uri} SIP/2.0", *(f"{key}: {value}" for key, value in headers.items()),
             f"Content-Length: {len(body.encode())}", "", body]
    return "\r\n".join(lines).encode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="192.168.45.10")
    parser.add_argument("--domain", default="freepbx.lab.lan")
    parser.add_argument("--username", default="500")
    parser.add_argument("--password", default="500")
    parser.add_argument("--number", default="100")
    parser.add_argument("--local-host", default="0.0.0.0")
    parser.add_argument("--advertise-host", default="10.10.255.11")
    parser.add_argument("--local-port", type=int, default=5062)
    args = parser.parse_args()
    target = (args.server, 5060)
    call_id, tag, cseq = secrets.token_hex(12), secrets.token_hex(6), 1

    def headers(method):
        return {
            "Via": f"SIP/2.0/UDP {args.advertise_host}:{args.local_port};branch=z9hG4bK{secrets.token_hex(8)};rport",
            "From": f"<sip:{args.username}@{args.domain}>;tag={tag}",
            "To": f"<sip:{args.username}@{args.domain}>",
            "Call-ID": call_id,
            "CSeq": f"{cseq} {method}",
            "Contact": f"<sip:{args.username}@{args.advertise_host}:{args.local_port}>",
            "Max-Forwards": "70",
        }

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((args.local_host, args.local_port))
        sock.settimeout(3)
        register_uri = f"sip:{args.domain}"
        sock.sendto(request("REGISTER", register_uri, headers("REGISTER")), target)
        data, _ = sock.recvfrom(65535)
        start, response_headers, _ = parse_message(data)
        print("REGISTER:", start)
        if start.startswith("SIP/2.0 401"):
            cseq += 1
            authenticated = headers("REGISTER")
            authenticated["Authorization"] = digest_authorization(
                response_headers["www-authenticate"], args.username, args.password, "REGISTER", register_uri)
            sock.sendto(request("REGISTER", register_uri, authenticated), target)
            data, _ = sock.recvfrom(65535)
            print("REGISTER authenticated:", parse_message(data)[0])

        cseq += 1
        invite_uri = f"sip:{args.number}@{args.domain}"
        invite = headers("INVITE")
        invite["To"] = f"<sip:{args.number}@{args.domain}>"
        invite["Content-Type"] = "application/sdp"
        sdp = (f"v=0\r\no=- 0 0 IN IP4 {args.advertise_host}\r\ns=probe\r\n"
               f"c=IN IP4 {args.advertise_host}\r\nt=0 0\r\nm=audio 40000 RTP/AVP 0\r\n")
        sock.sendto(request("INVITE", invite_uri, invite, sdp), target)
        data, _ = sock.recvfrom(65535)
        print("INVITE:", parse_message(data)[0])


if __name__ == "__main__":
    main()
