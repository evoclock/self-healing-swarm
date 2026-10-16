ALTER TABLE tenants ADD COLUMN gateway_project_id text;

UPDATE tenants SET gateway_project_id = '5c910b17-02b3-4fd1-9887-99bc8f532ad5' WHERE tenant_id = 't-a';
UPDATE tenants SET gateway_project_id = 'c617fb60-78a9-468a-84d5-0a4a75856591' WHERE tenant_id = 't-b';
UPDATE tenants SET gateway_project_id = '67e82ab5-c937-493b-94cc-1d3bac05280c' WHERE tenant_id = 't-c';
UPDATE tenants SET gateway_project_id = '219aeb0b-7d78-4998-ab1a-d0d9b4764883' WHERE tenant_id = 't-d';
