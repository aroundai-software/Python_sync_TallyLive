import requests

xml = """<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Company Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVCURRENTCOMPANY>GROUP LAVENDER S</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Company Collection">
                        <TYPE>Company</TYPE>
                        <FETCH>Name</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""

print("=== EXPLICIT COMPANY FETCH ===")
try:
    print(requests.post('http://localhost:9000', data=xml).text)
except Exception as e:
    print(e)
