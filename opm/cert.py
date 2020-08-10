import re
from twisted.internet import defer, error, protocol, ssl, interfaces
from zope.interface   import directlyProvides


class CertificateProtocol(protocol.Protocol):
    def __init__(self, bad):
        self.bad = bad
        directlyProvides(self, interfaces.IHandshakeListener)

    def connectionMade(self):
        self.deferred = defer.Deferred(self.cancel)
    def cancel(self, defer):
        if defer is self.deferred:
            self.deferred = None
            self.transport.loseConnection()
    def connectionLost(self, reason):
        if self.deferred is not None:
            self.deferred.callback(None)

    def handshakeCompleted(self):
        values  = []
        cert    = ssl.Certificate(self.transport.getPeerCertificate())

        if cert is not None:
            subject = {k: v.decode('utf8') for k, v in cert.getSubject().items()}
            if 'commonName' in subject:
                values.append(('scn', subject['commonName']))
            if 'organizationName' in subject:
                values.append(('son', subject['organizationName']))

            for i in range(cert.original.get_extension_count()):
                ext = cert.original.get_extension(i)
                if ext.get_short_name() == b"subjectAltName":
                    sans = str(ext).split(", ")
                    sans = [s.split(":", 1)[1] for s in sans]
                    for san in sans:
                        values.append(("san", san))

        for pattern, description in self.bad:
            for k, v in values:
                key = f'{k}:{v.lower()}'
                if pattern.fullmatch(key):
                    d = self.deferred
                    self.deferred = None
                    d.callback(description)
                    break

        self.transport.loseConnection()

class CertificateChecker(object):
    def __init__(self, port, bad, message):
        self.port = port
        # convert {k:v} to [(regex(k), v)]
        self.bad  = [(re.compile(k, re.I), v) for k, v in bad.items()]
        self.msg  = message

    def check(self, scan, env):
        options = ssl.CertificateOptions(verify=False)
        creator = protocol.ClientCreator(env.reactor, CertificateProtocol,
                                         self.bad)

        if env.bind_address:
            bindAddress = (env.bind_address, 0)
        else:
            bindAddress = None
        # Disable the timeout here because our calling scanner should
        # cancel us just fine without it:
        d = creator.connectSSL(scan.ip, self.port, options, timeout=None,
                               bindAddress=bindAddress)

        def gotDescription(description):
            if description is not None:
                return self.msg.format(desc=description)
            else:
                return None
        def connected(proto):
            return proto.deferred.addCallback(gotDescription)
        def connectFailed(fail):
            # If we could not connect for some sane reason it's just
            # not a proxy. Let unknown errors propagate though.
            fail.trap(error.ConnectionRefusedError, error.TCPTimedOutError)
        d.addCallbacks(connected, connectFailed)

        return d