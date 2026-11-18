from html.parser import HTMLParser


class InstructionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.tokens.append(("start", tag, sorted(attrs)))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        self.tokens.append(("end", tag))
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if data.isspace() and self.stack and self.stack[-1] in {"ol", "ul"}:
            return
        self.tokens.append(("text", data))

    def handle_comment(self, data):
        self.tokens.append(("comment", data))


def instruction_tokens(value):
    parser = InstructionParser()
    parser.feed(value)
    parser.close()
    return parser.tokens
